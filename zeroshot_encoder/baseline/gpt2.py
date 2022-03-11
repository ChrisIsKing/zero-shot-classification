"""
Implementation of NVIDIA-GPT2 approach.

[Zero-shot Text Classification With Generative Language Models](https://arxiv.org/abs/1912.10165)
"""
from warnings import warn
from collections import defaultdict

from tqdm import tqdm
from torch import nn
from torch.utils.data import DataLoader
from sklearn.metrics import classification_report
import transformers
from transformers import BatchEncoding
from transformers import AutoConfig
from transformers import GPT2TokenizerFast
from transformers import GPT2Model, GPT2LMHeadModel  # LMHead for CLM training
from transformers import Trainer, TrainingArguments, SchedulerType
from transformers import DataCollatorForLanguageModeling
from transformers.training_args import OptimizerNames
from datasets import load_metric, load_dataset

from zeroshot_encoder.util import *
from zeroshot_encoder.preprocess import get_dset


MODEL_NAME = 'gpt2-nvidia'


class ZsGPT2Tokenizer(GPT2TokenizerFast):
    """
    A wrapper around GPT2 tokenizer for 0-shot classification tokenizing
    """
    SPEC_TOKS = OrderedDict([
        ('pref_ques', '<|question|>'),  # Word embeddings
        ('pref_text', '<|text|>'),
        ('pref_answ', '<|answer|>'),
        ('type_ques', '[QUES]'),  # Type embeddings
        ('type_text', '[TEXT]'),
        ('type_answ', '[ANSW]')
    ])

    class Cache(dict):
        """
        Wrapper around caching dict, that loads metadata on corresponding dataset
        """
        def __init__(self, tokenizer: 'ZsGPT2Tokenizer'):
            super().__init__()
            self.tokenizer = tokenizer
            self.tpl_grouped = re.compile(rf'^(?P<dataset_name>.?)-label-grouped$')

        def __getitem__(self, key: Tuple[str, str]):
            """
            :param key: 2-tuple of (dataset_name, split)

            Needed cos huggingface may load cached dataset, internal cache is gone

            .. note:: works for local disk dataset only
            """
            dataset_name, split = key
            key = f'{dataset_name}-{split}'
            if key not in self:
                dset = datasets.load_from_disk(
                    os.path.join(get_output_base(), DIR_PROJ, DIR_DSET, 'processed', dataset_name)
                )[split]
                # See `zeroshot_encoder.util.util.py::process_utcd_dataset`
                is_multi_label = 'labels' in dset.features.keys()
                feats = dset.features['labels' if is_multi_label else 'label']
                n_cls = feats.num_classes
                if is_multi_label:
                    dataset_name = self.tpl_grouped.match(dataset_name).group('dataset_name')
                assert feats.names == config(f'UTCD.datasets.{dataset_name}.splits.{split}.labels')  # sanity check
                label2description: Dict[int, str] = {i: desc for i, desc in enumerate(feats.names)}  # label is index
                self[key] = dict(
                    n_classes=n_cls, label2description=label2description,
                    max_label_id_length=max(len(self.tokenizer._call_paren(lb)) for lb in feats.names)
                )
            return super().__getitem__(key)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Pad token cannot be `self.eos_token`
        # cos otherwise `DataCollatorForLanguageModeling` would override normal eos tokens
        self.add_special_tokens(dict(
            pad_token='[PAD]', additional_special_tokens=list(ZsGPT2Tokenizer.SPEC_TOKS.values())
        ))

        self.templates = config('baselines.gpt2-nvidia.templates')
        # Mapping from dataset name to label for non-UTCD cases
        self.cache: Dict[str, Dict] = ZsGPT2Tokenizer.Cache(self)
        self.cache_bm = None

        self.boq_token, self.bot_token, self.boa_token = (  # begin of (question, text, answer) tokens
            ZsGPT2Tokenizer.SPEC_TOKS[k] for k in ('pref_ques', 'pref_text', 'pref_answ')
        )  # Special tokens
        self.question_type_token, self.text_type_token, self.answer_type_token = (
            ZsGPT2Tokenizer.SPEC_TOKS[k] for k in ('type_ques', 'type_text', 'type_answ')
        )  # Type tokens

        self.warned_desc = set()  # Warning for each dataset happens once    @property

    @property
    def max_len_single_sentence(self) -> int:
        return self.model_max_length - 2 * 3  # 3 pairs of (special start token, eos token)

    def _call_paren(self, s: str, **kwargs) -> List[int]:
        return super().__call__(s, **kwargs)['input_ids']

    def enc_spec(self, tok: str) -> int:
        """
        Encode special tokens with sanity check
        """
        id_ = self.encode(tok)
        assert len(id_) == 1
        return id_[0]  # Intended for special tokens

    def __call__(
            self, samples: Dict[str, Union[List, str, int]],
            dataset_name: str = 'UTCD', mode: str = 'train', for_prediction: bool = False,
            **kwargs
    ):
        """
        :param samples: Data sample(s) with keys [`dataset_name`, `label`, `text`]
            Each value an element or a list of elements
        :param for_prediction: If true, the answer part is not tokenized,
            the text portion is truncated such that the label with largest # of ids may be generated;
            the batch is not padded
                i.e. Intended for prediction, see `evaluate_trained`
        """
        max_length = kwargs.get('max_length', None)
        is_batched = isinstance(samples['text'], (tuple, list))
        if max_length is None:
            max_length = self.model_max_length
        n_token = self.model_max_length  # Indented number of token positions as in the actual architecture

        ln = len(samples['text'])
        idxs_tpl = np.random.randint(len(self.templates), size=ln)

        def call_single(i, dataset_id: int, text: str, label: int):
            dset_nm: str = config('UTCD.dataset_id2name')[dataset_id]
            if 'UTCD' in dataset_name:
                split = 'train' if mode == 'train' else 'test'
                descs = config(f'UTCD.datasets.{dset_nm}.splits.{split}.labels')  # Descriptive labels
                n_cls = len(descs)
                # `label` is shared across all datasets, map to local label within dataset
                if self.cache_bm is None:
                    self.cache_bm = datasets.load_from_disk(
                        os.path.join(get_output_base(), DIR_PROJ, DIR_DSET, 'processed', dataset_name)
                    )[split].features['label']  # TODO: assume `train` split
                # The ordering indicates int<=>str label mapping, i.e., index is int label,
                # see `process_utcd_dataset`

                def lb_int2desc(lb: int) -> str:
                    """
                    Map from local dataset label ordinal, in range(n_cls) to the descriptor
                    """
                    return descs[lb]
                answer = self.cache_bm.int2str(label)
            else:
                self.cache: ZsGPT2Tokenizer.Cache
                n_cls, label2description = (self.cache[dset_nm, mode][k] for k in ('n_classes', 'label2description'))

                def lb_int2desc(lb: int) -> str:
                    return label2description[lb]
                if for_prediction:
                    answer = ''  # indexing wouldn't work cos multi label; Will not be used anyway, see below
                else:
                    answer = label2description[label]

            idx_lbs = np.arange(n_cls)
            np.random.shuffle(idx_lbs)
            strs_lb = ' , '.join(f'" {lb_int2desc(idx)} "' for idx in idx_lbs)
            question = self.templates[idxs_tpl[i]].format(strs_lb)

            ids_ques = self._call_paren(question, **kwargs)
            ids_text = self._call_paren(text, **kwargs)
            ids_answ = self._call_paren(answer, **kwargs)
            ln_q, ln_t, ln_a = len(ids_ques), len(ids_text), len(ids_answ)

            if for_prediction:
                ln_cont = (1+ln_q+1) + (1+ln_t+1) + 1  # for `pref_answ`
                max_label_id_length = self.cache[dset_nm, mode]['max_label_id_length']
                # The maximum number of tokens that could fit for context/prompt
                room = self.model_max_length-1 - max_label_id_length  # Also needs to generate `EOS`
                if ln_cont > room:
                    # Crop the text portion so that the longest label can be generated
                    ln_t_ = room - ((1+ln_q+1) + (1+1) + 1)
                    assert ln_t_ > 0
                    warn(f'Sample without answer longer than model max sequence length and dataset {dset_nm} labels: '
                         f'{ln_cont} > {self.model_max_length} - Text portion cropped: {ln_t} > {ln_t_} for inference')
                    ids_text = ids_text[:ln_t_]
            else:
                ln_ids = ln_q + ln_t + ln_a
                if ln_ids > self.max_len_single_sentence:
                    # Crop the text portion, keep question and label intact,
                    # i.e., ensure no classification label is cropped
                    ln_t_ = self.max_len_single_sentence - (ln_q + ln_a)
                    assert ln_t_ > 0
                    warn(f'Sample with answer longer than model max sequence length for dataset {dset_nm}: '
                         f'{ln_ids+6} > {self.model_max_length} - Text portion cropped: {ln_t} > {ln_t_} for training')
                    ids_text = ids_text[:ln_t_]
            # Number of contex tokens, up until answer token, inclusive
            n_ques, n_text, n_answ = (1+len(ids_ques)+1), (1+len(ids_text)+1), (1+len(ids_answ)+1)
            n_cont = n_ques + n_text + 1
            ids = [
                self.enc_spec(self.boq_token), *ids_ques, self.enc_spec(self.eos_token),
                self.enc_spec(self.bot_token), *ids_text, self.enc_spec(self.eos_token),
                self.enc_spec(self.boa_token), *ids_answ, self.enc_spec(self.eos_token)
            ]
            tids = [self.enc_spec(self.question_type_token)] * n_ques + \
                   [self.enc_spec(self.text_type_token)] * n_text + \
                   [self.enc_spec(self.answer_type_token)] * n_answ
            if for_prediction:
                ids, tids = ids[:-(n_answ-1)], tids[:-(n_answ-1)]
                assert len(ids) == (n_ques+n_text+1)  # sanity check
            msks = [1] * len(ids)  # Encode ids are attended for CLM
            # Context position ids, followed by output position ids
            # adding `n_token` offset for the modified positional embeddings, see `ZsGPT2Model`
            pids = list(range(n_cont)) + [i + n_token for i in range(len(ids)-n_cont)]
            assert all(len(lst_ids) == len(ids) for lst_ids in (ids, tids, msks, pids))  # Sanity check

            def pad(ints: List[int], name) -> List[int]:
                """
                Pad to max_length, truncate if necessary
                """
                if name == 'attention_mask':
                    int_pad = 0  # Ignore in attention
                elif name == 'position_ids':
                    # Arbitrary, since will be ignored, but needs to be within `n_token` for embedding mapping
                    int_pad = 0
                else:
                    # `input_id`s set to `pad_token` will be ignored by `DataCollatorForLanguageModeling`
                    int_pad = self.enc_spec(self.pad_token)
                return ints[:max_length] if len(ints) > max_length else (ints + [int_pad] * (max_length - len(ints)))
            out = {k: (ints if for_prediction else pad(ints, k)) for k, ints in ((
                ('input_ids', ids), ('attention_mask', msks), ('token_type_ids', tids), ('position_ids', pids)
            ))}
            out['dataset_id'] = dataset_id  # For computing zero-shot classification accuracy
            return out
        # See `zeroshot_encoder.util.util.py::process_utcd_dataset`
        k_label = 'label' if 'label' in samples else 'labels'
        if is_batched:
            ds = [call_single(i, d_id, txt, lb) for i, (d_id, txt, lb) in enumerate(zip(
                *[samples[k] for k in ['dataset_id', 'text', k_label]]
            ))]
            return BatchEncoding({k: [d[k] for d in ds] for k in ds[0]})  # Stack all the ids
        else:
            return BatchEncoding(call_single(0, *[samples[k] for k in ['dataset_id', 'text', k_label]]))


class ZsGPT2Model(GPT2Model):
    """
    Modifying the `GPT2Model` for 0-shot classification paper
    """
    def __init__(self, config_):
        super().__init__(config_)
        # Override internal state, instead of adding internal state, so that forward pass stays untouched
        # Double the positional embedding matrix, as if stacking the context & output embedding matrices together
        # See positional id assignment in `ZsGPT2Tokenizer`
        self.wpe = nn.Embedding(config_.max_position_embeddings*2, self.embed_dim)


def pprint_gpt2_input(tokenizer: ZsGPT2Tokenizer, d: Dict[str, torch.Tensor]):
    """
    Prints to console the encoded ids, positional ids and type ids as sanity check
    """
    n_ct, n_dnm, n_wd = 3, 10, 13
    n_pad = n_ct + n_dnm + 3
    ids, pids, tids, dids = (d[k].detach() for k in ('input_ids', 'position_ids', 'token_type_ids', 'dataset_id'))
    pad = tokenizer.enc_spec(tokenizer.pad_token)
    id2name = config('UTCD.dataset_id2name')

    for i, (ids_, did, pids_, tids_) in enumerate(zip(ids, dids, pids, tids)):
        msk = (ids_ != pad)
        ids_, pids_, tids_ = ids_[msk], pids_[msk], tids_[msk]
        print(f'{i:>{n_ct}}: {id2name[did.item()]:>{n_dnm}}', end=' ')
        for id_ in ids_:
            tok = tokenizer.decode(id_)
            print(f'{tok:>{n_wd}}', end='')
        print()

        print(' ' * n_pad, end='')
        for pid in pids_:
            print(f'{pid.item():>{n_wd}}', end='')
        print()
        print(' ' * n_pad, end='')
        for tid in tids_:
            print(f'{tokenizer.decode(tid):>{n_wd}}', end='')
        print()


class ZsGPT2LMHeadModel(GPT2LMHeadModel):
    """
    So that `ZsGPT2Model` is loaded
    """
    def __init__(self, config_):
        super().__init__(config_)
        self.transformer = ZsGPT2Model(config_)  # Override internal state

    def forward(self, dataset_id=None, **kwargs):
        # Function override to ignore `dataset_id`, not need in learning; Just need to pass value for evaluation
        # pprint_gpt2_input(kwargs | dict(dataset_id=dataset_id))
        # exit(1)
        return super().forward(**kwargs)

    @classmethod
    def from_pretrained(cls, *args, is_zs_gpt2: bool = False, **kwargs):
        """
        :param is_zs_gpt2: If True, loads a local `ZsGPT2LMHeadModel`; otherwise, expects a GPT2 model
        """
        if is_zs_gpt2:
            return super().from_pretrained(*args, **kwargs)
        else:
            md_ = super().from_pretrained(*args, **kwargs)  # Loads the GPT2LMHeadModel while ignoring `wpe.weight`
            md_ori = GPT2LMHeadModel.from_pretrained(*args, **kwargs)
            weight_pretrained = md_ori.transformer.wpe.state_dict()['weight']
            # Check `vars(md_ori.transformer.wpe)`, weight is the only parameter
            del md_ori

            # Crude loading the pretrained weights, to each half of the doubled positional embedding
            with torch.no_grad():
                n_tok = md_.transformer.wpe.weight.shape[0]
                if n_tok == 1024 * 2:
                    md_.transformer.wpe.weight[:1024, :] = weight_pretrained
                    md_.transformer.wpe.weight[1024:, :] = weight_pretrained
                else:
                    warn('Wrong model size, positional not loaded. This is expected in debugging')
            return md_


def tokenize_func(
        tokenizer_: ZsGPT2Tokenizer, dataset_name='ag_news', max_length=None,
        mode: str = 'train', for_prediction: bool = False
):
    def _tokenize_func(sample: Dict[str, List]):
        """
        :param sample: A batch of data samples
        """
        if 'UTCD' not in dataset_name:
            # if not hasattr(_tokenize_func, 'dataset_id_cache'):
            #     _tokenize_func.dataset_id_cache = dict()
            # if dataset_name not in _tokenize_func.dataset_id_cache:
            #     split = 'train' if mode == 'train' else 'test'
            #     k_label = 'labels' if config(f'UTCD.dataset.{dataset_name}.splits.') else 'label'
            #     _tokenize_func.dataset_id_cache[dataset_name]
            # ic(sample.keys())
            # exit(1)
            # print(dataset_name)
            # print(type(sample))
            # print(len(sample['text']))
            sample['dataset_id'] = [config('UTCD.dataset_name2id')[dataset_name]] * len(sample['text'])
            # exit(1)
        # Otherwise, `dataset_id` already part of input
        return tokenizer_(
            sample, dataset_name=dataset_name, max_length=max_length, mode=mode, for_prediction=for_prediction
        )
    return _tokenize_func


def get_model_n_tokenizer(model_name='gpt2', save_gpu_memory: bool = True) -> Tuple[
    ZsGPT2LMHeadModel, ZsGPT2Tokenizer, DataCollatorForLanguageModeling
]:
    pretrained_model_name = 'gpt2'

    if 'debug' in model_name:  # Try a smaller model for training sanity check
        if 'large' in model_name:
            n_token = 128
        else:
            n_token = 4
        conf = AutoConfig.from_pretrained('gpt2')
        # If using cpu, must be debugging and hence no `gradient_checkpointing`, see `get_train_setup`
        conf.update(dict(n_ctx=n_token, n_positions=n_token, use_cache=not torch.cuda.is_available()))
        model_ = ZsGPT2LMHeadModel.from_pretrained(pretrained_model_name, config=conf, ignore_mismatched_sizes=True)
        model_max_length = n_token
    else:
        model_max_length = 1024  # Keep max seq len of 1024, instead of 512 in paper, for longer texts & more labels
        conf = AutoConfig.from_pretrained(model_name)
        # `use_cache` in compatible with `gradient_checkpointing`, see `get_train_setup`
        conf.update(dict(use_cache=not (torch.cuda.is_available() and save_gpu_memory)))
        # Keep the 1024 token length, reducing to 512 tokens involves loading part of pretrained weights, complicated
        model_ = ZsGPT2LMHeadModel.from_pretrained(model_name, config=conf, ignore_mismatched_sizes=True)

    tokenizer_ = ZsGPT2Tokenizer.from_pretrained(
        pretrained_model_name, use_fast=True, model_max_length=model_max_length
    )
    model_.resize_token_embeddings(len(tokenizer_))

    return model_, tokenizer_, DataCollatorForLanguageModeling(tokenizer=tokenizer_, mlm=False)


def get_train_setup(
        model_name='gpt2', do_eval=True, train_args: Dict = None,
        save_gpu_memory: bool = True
) -> TrainingArguments:
    name_ = model_name
    if name_ == 'debug-gpt-ori':
        name_ = 'gpt2'

    d_train_args = {
        'debug': dict(
            learning_rate=1e-4,
            batch_size=4,
            weight_decay=1e-2,
            num_train_epochs=4,
            lr_scheduler_type=SchedulerType.CONSTANT,
        ),
        'debug-large': dict(
            learning_rate=5e-5,
            batch_size=4,
            weight_decay=1e-2,
            num_train_epochs=40,
            lr_scheduler_type=SchedulerType.CONSTANT,
        ),
        'gpt2': dict(
            learning_rate=3e-5,
            batch_size=32,
            weight_decay=1e-2,
            num_train_epochs=5,
            lr_scheduler_type=SchedulerType.COSINE,
        ),
        'gpt2-medium': dict(
            learning_rate=4e-5,
            train_batch_size=16,
            eval_batch_size=40,
            gradient_accumulation_steps=8,  # To fit in memory; Effectively batch size 128 as in paper
            weight_decay=1e-2,
            num_train_epochs=10,
            lr_scheduler_type=SchedulerType.COSINE,
        )
    }
    lr, bsz, decay, n_ep, sch, gas = (d_train_args[name_].get(k, None) for k in [
        'learning_rate', 'batch_size', 'weight_decay',
        'num_train_epochs', 'lr_scheduler_type', 'gradient_accumulation_steps'
    ])
    if bsz is None:
        bsz_tr, bsz_vl = (d_train_args[name_].get(k, None) for k in ('train_batch_size', 'eval_batch_size'))
        assert bsz_tr is not None and bsz_vl is not None
    else:
        bsz_tr = bsz_vl = bsz
    if torch.cuda.is_available():
        bsz_tr /= torch.cuda.device_count()  # Distribute among GPUs
        assert bsz_tr.is_integer()
        bsz_tr = int(bsz_tr)
    args = dict(
        output_dir=os.path.join(get_output_base(), DIR_PROJ, DIR_MDL, 'gpt2', model_name, now(sep='-')),
        do_train=True,
        do_eval=do_eval,
        evaluation_strategy='steps' if do_eval else 'no',
        per_device_train_batch_size=bsz_tr,
        per_device_eval_batch_size=bsz_vl,
        gradient_accumulation_steps=gas,
        eval_accumulation_steps=128,  # Saves GPU memory
        # Adam's beta1, beta2, epsilon taken from the GPT2 config in
        # https://github.com/huggingface/transformers/blob/master/examples/pytorch/language-modeling/run_clm.py
        learning_rate=lr,
        weight_decay=decay,
        adam_beta1=0.9,
        adam_beta2=0.999,
        adam_epsilon=1e-08,
        max_grad_norm=1,
        num_train_epochs=n_ep,
        lr_scheduler_type=sch,
        warmup_ratio=1e-2,
        log_level='info',
        # log_on_each_node=False,
        log_level_replica='info',
        logging_strategy='steps',
        logging_steps=1,
        save_strategy='epoch',
        fp16=torch.cuda.is_available(),
        fp16_full_eval=True,
        # fp16_full_eval=False,  # As in doc, harms metric
        optim=OptimizerNames.ADAMW_TORCH,
        disable_tqdm=True,
        # Pass dataset name information down to `compute_loss` for computing text classification accuracy
        remove_unused_columns=False,
        report_to='none',
        # Set to True on CPU gives warning; Enable for fitting in `clarity1` memory
        gradient_checkpointing=torch.cuda.is_available() and save_gpu_memory
    )
    if train_args is None:
        train_args = dict()
    args = {k: v for k, v in args.items() if v is not None}
    args.update(train_args)
    return TrainingArguments(**args)


def compute_metrics(eval_pred: MyEvalPrediction):
    """
    Will be called on eval data only, **during training**
    """
    # Intended to work with `CustomTrainer.prediction_step`
    if not hasattr(compute_metrics, 'metric'):
        compute_metrics.metric = load_metric('accuracy')
    # Labels are per-sample already, see `CustomTrainer.prediction_step`
    preds, trues, dids = eval_pred.predictions, eval_pred.label_ids, eval_pred.dataset_ids
    # id2dnm = config('UTCD.dataset_id2name')
    # path_dir = os.path.join(PATH_BASE, DIR_PROJ, 'evaluations', MODEL_NAME, now(sep='-'))
    # os.makedirs(path_dir, exist_ok=True)
    #
    # for did in np.unique(dids):
    #     dnm_ = id2dnm[did]
    #     # TODO: only evaluation split for now
    #     id_label, desc_label = zip(*enumerate(config(f'UTCD.datasets.{dnm_}.labels.test')))  # Label is index
    #     msk_dset = (dids == did)
    #     preds_, trues_ = preds[msk_dset], trues[msk_dset]
    #     df = pd.DataFrame(
    #         # note `-1` is not actual label, support of 0 - included for full label specification per sklearn
    #         # **note** cos the -1 label, the `macro avg` row is not accurate; included it for getting global accuracy
    #         classification_report(
    #             trues_, preds_, labels=[-1, *id_label], target_names=['Label not in dataset', *desc_label],
    #             output_dict=True
    #         )
    #     ).transpose()
    #     df.to_csv(os.path.join(path_dir, f'{dnm_}.csv'))
    return compute_metrics.metric.compute(predictions=preds, references=trues)


def get_all_setup(
        model_name, dataset_name: str = 'ag_news',
        n_sample=None, random_seed=None, do_eval=True, custom_logging=True,
        train_args: Dict = None
) -> Tuple[GPT2LMHeadModel, Union[GPT2TokenizerFast, ZsGPT2Tokenizer], datasets.Dataset, datasets.Dataset, Trainer]:
    if model_name == 'debug-gpt-ori':  # Sanity check: As if keep training GPT-2, with padding for simplicity
        conf = AutoConfig.from_pretrained('gpt2')
        conf.update(dict(use_cache=False))
        model_ = GPT2LMHeadModel.from_pretrained('gpt2', config=conf)
        tokenizer_ = GPT2TokenizerFast.from_pretrained('gpt2')
        data_collator_ = None
        train_args_ = get_train_setup(model_name, do_eval=do_eval)

        def group_texts(examples):
            examples = tokenizer_(examples['text'])
            # Taken from
            # https://github.com/huggingface/notebooks/blob/master/examples/language_modeling_from_scratch.ipynb
            # block_size = tokenizer_.model_max_length
            block_size = 512  # To fit in memory
            concatenated_examples = {k: sum(examples[k], []) for k in examples.keys()}
            total_length = len(concatenated_examples[list(examples.keys())[0]])
            total_length = (total_length // block_size) * block_size
            result = {
                k: [t[i: i + block_size] for i in range(0, total_length, block_size)]
                for k, t in concatenated_examples.items()
            }
            result['labels'] = result['input_ids'].copy()
            return result
        tr_map_func = vl_map_func = group_texts
    else:
        save_gpu_mem = 'arc-ts' not in get_hostname()
        # save_gpu_mem = True  # Gradient checkpointing still needed - otherwise doesn't fit in 44G GPU
        model_, tokenizer_, data_collator_ = get_model_n_tokenizer(model_name, save_gpu_memory=save_gpu_mem)
        train_args_ = get_train_setup(model_name, do_eval=do_eval, train_args=train_args, save_gpu_memory=save_gpu_mem)
        tr_map_func = tokenize_func(tokenizer_, dataset_name=dataset_name, mode='train')
        vl_map_func = tokenize_func(tokenizer_, dataset_name=dataset_name, mode='test')

    dset_tr_, dset_vl_ = get_dset(
        dataset_name=dataset_name,
        d_map_func=dict(train=tr_map_func, test=vl_map_func), remove_columns=['label', 'text'],
        n_sample=n_sample, random_seed=random_seed,
        fast='debug' not in model_name
    )
    trainer_args = dict(
        model=model_, args=train_args_, data_collator=data_collator_,
        train_dataset=dset_tr_, eval_dataset=dset_vl_, compute_metrics=compute_metrics
    )
    trainer_ = CustomTrainer(
        tokenizer=tokenizer_, custom_logging=custom_logging, compute_cls_acc=model_name != 'debug-gpt-ori',
        **trainer_args
    )
    return model_, tokenizer_, dset_tr_, dset_vl_, trainer_


def load_trained(epoch: int = 3) -> ZsGPT2LMHeadModel:
    assert epoch in [2, 3]
    if not hasattr(load_trained, 'epoch2path'):
        load_trained.epoch2path = {
            2: os.path.join(
                PATH_BASE, DIR_PROJ, 'trained-models', 'gpt2-nvidia', '2022-03-04 21-33-12', 'checkpoint-37066'
            ),
            3: os.path.join(
                PATH_BASE, DIR_PROJ, 'trained-models', 'gpt2-nvidia', '2022-03-04 21-33-12', 'checkpoint-55599'
            )
        }
    checkpoint_path = load_trained.epoch2path[epoch]
    return ZsGPT2LMHeadModel.from_pretrained(checkpoint_path, is_zs_gpt2=True).to('cuda')  # with caching


def evaluate_trained(in_domain: bool = True, batch_size: int = 48):
    """
    Run evaluation, on potentially multi-label datasets
    """
    model = load_trained(epoch=3).to('cuda')
    ic(ZsGPT2LMHeadModel.__mro__)
    # To disable warning `Setting `pad_token_id` to `eos_token_id`:50256 for open-end generation.`; TODO
    model.config.max_length = model.config.n_ctx
    model.config.pad_token_id = model.config.eos_token_id
    model.eval()
    tkzer = ZsGPT2Tokenizer.from_pretrained('gpt2', use_fast=True, model_max_length=model.config.n_ctx)
    # tkzer.pad_token_id = tkzer.eos_token_id
    # data_collator = DataCollatorForLanguageModeling(tokenizer=tkzer, mlm=False)

    split = 'test'
    path_dir = os.path.join(PATH_BASE, DIR_PROJ, 'evaluations', MODEL_NAME, now(sep='-'))
    logger_name = 'GPT2-NVIDIA Evaluation'
    logger = get_logger(logger_name, typ='stdout')
    logger_fl = get_logger(
        f'{logger_name} file-write', typ='file-write', file_path=os.path.join(path_dir, f'{logger_name}.log')
    )
    ic(logger.handlers, logger_fl.handlers)
    from transformers.generation_utils import GenerationMixin
    logger.info(f'Running evaluation {logi("in domain" if in_domain else "out of domain")}... ')
    logger_fl.info(f'Running evaluation {"in domain" if in_domain else "out of domain"}... ')

    for dnm_, d in config('UTCD.datasets').items():
        if d['out_of_domain'] == (not in_domain):
            ic(dnm_)
            if dnm_ != 'multi_eurlex':  # TODO: debugging
                continue
            d_info = config(f'UTCD.datasets.{dnm_}.splits.{split}')
            is_multi_label = d_info['multi_label']
            lb2id = defaultdict(lambda: -1)  # If generated invalid descriptive label, will return -1
            labels = d_info['labels']
            # predictions and label descriptions all to lower case to be more lenient
            lb2id.update({lb.lower(): i for i, lb in enumerate(labels)})
            ic(lb2id)
            dnm_disk = f'{dnm_}-label-grouped' if is_multi_label else dnm_
            dset = get_dset(  # Get evaluation set only
                dataset_name=dnm_disk, splits='test',
                d_map_func=dict(test=tokenize_func(tkzer, dataset_name=dnm_, mode='test', for_prediction=True)),
                remove_columns='text', n_sample=None, from_disk=True
            )[0]

            # Hack to order the dataset by length of input ids -
            # for batched generation, that **doesn't take up padding, this is not supported by HuggingFace**
            # cnm_lid = 'len_ids'
            # dset = dset.add_column(cnm_lid, [len(ids) for ids in dset[:]['input_ids']])
            # ic(dset[0])
            # ic(dset[1, 3, 5])
            # dset = dset.sort(column=cnm_lid)
            # ic(dset[0])
            # labels_true = dset[:]['labels']
            # ic(labels_true)
            # For final metric computation, the correct label will be selected based on prediction
            trues, preds = np.empty(len(dset), dtype=int), np.empty(len(dset), dtype=int)
            # ic(trues, preds)
            len_ids = np.array([len(ids) for ids in dset[:]['input_ids']])
            # dset = dset.remove_columns(cnm_lid)
            # ic(dset[:3])
            # ic(len_ids, len_ids.shape, np.unique(len_ids))
            uniq_lens = np.unique(len_ids)
            # from collections import Counter
            # ic(Counter(len_ids))
            # dl = DataLoader(dataset=dset, batch_size=48, collate_fn=data_collator)
            # ic(len(dl))
            # for step, input_ in tqdm(enumerate(dl)):
            #     ic(input_.keys())
            #     ic(input_)
            #     output = model(**input_)
            #     ic(output.keys())
            #     exit(1)
            # for len_ids, samples in itertools.groupby(dset, key=lambda sample: sample[cnm_lid]):
            #     ic(len_ids, samples, type(samples))
            # Batches of likely different sizes
            ln2idxs = [np.where(len_ids == ln)[0] for ln in uniq_lens]
            # ic([len(idxs) for idxs in ln2idxs])
            idxs_batches = sum(
                (np.split(idxs, range(batch_size, idxs.size, batch_size)) if idxs.size > batch_size else [idxs]
                 for idxs in ln2idxs),
                start=[]
            )
            n_bch = len(idxs_batches)
            logger.info(f'Running evaluation on dataset {logi(dnm_disk)} of {logi(len(dset))} unique texts '
                        f'in {logi(n_bch)} batches... ')
            logger_fl.info(f'Running evaluation on dataset {dnm_disk} of {len(dset)} unique texts '
                           f'in {n_bch} batches... ')
            # ic([len(idxs) for idxs in idxs_batches])
            # correct_label_ids = set(range(len(labels)))

            # for idxs in tqdm(idxs_batches, unit='ba'):  # Each batch has input samples of the same token length
            for step, idxs in enumerate(idxs_batches):  # Each batch has input samples of the same token length
                # dset_ = dset.select(np.where(len_ids == ln)[0])  # `Dataset.select` works with integer indices only
                idxs = [int(idx) for idx in idxs]
                # inputs = data_collator.torch_call([dset[idx] for idx in idxs])  # Use it for padding & Tensor conversion
                inputs = {  # Don't need to the labels to complicate forward pass
                    k: torch.tensor(v, device='cuda') for k, v in dset[idxs].items() if k not in ['label', 'labels']
                }
                # trues = dset[idxs]['labels' if is_multi_label else 'label']

                # ic(inputs['input_ids'].shape)
                # ic(inputs.keys())
                # ic(inputs['input_ids'].device)
                # ic(model.config)
                # ic(model.config.max_length)
                # ic(model.generate)
                # exit(1)
                outputs = model.generate(**inputs)  # Greedy decoding
                # ic(outputs.shape, outputs[:, -15:])
                # ic(outputs, trues)
                outputs = tkzer.batch_decode(outputs, skip_special_tokens=False)
                # ic(outputs)

                def set_pred_n_true(generated: str, i_sample: int) -> Tuple[int, int]:
                    # ic(tkzer.boa_token, tkzer.eos_token, i_sample, lb2id)
                    idxs_boa = get_substr_indices(s=generated, s_sub=tkzer.boa_token)
                    # there will be at least one index, as in prompt
                    answer_with_eos = generated[idxs_boa[0] + len(tkzer.boa_token):]
                    id_pred = -1
                    if len(idxs_boa) > 1:
                        # ic(idxs_boa)
                        # ic(generated)
                        # ic(inputs['input_ids'].shape)
                        # ic(inputs['input_ids'][:, -4:])
                        # ic(tkzer.boa_token)
                        # ic(tkzer.boa_token_id)
                        logger.warning(f'{model.__class__.__qualname__} generated {len(idxs_boa)} boa_token '
                                       f'instead of {1} with [{answer_with_eos}]')
                        logger_fl.warning(log_dict(d_log, with_color=False))
                    else:
                        assert len(idxs_boa) == 1
                        idxs_eot = get_substr_indices(s=answer_with_eos, s_sub=tkzer.eos_token)
                        if not len(idxs_eot):  # No eos token generated
                            # print(generated, answer_with_eos)
                            warn(f'{model.__class__.__qualname__} didn\'t finish generating answer '
                                 f'with [{answer_with_eos}]')
                        # if not (len(idxs_boa) == 1 and len(idxs_eot) == 3):
                        #     ic(i_sample, generated)
                        # assert len(idxs_eot) == 1
                        # GPT2 would generate multiple `eos_token` for the samples in the batch that terminates early
                        # assert len(idxs_eot) >= 1
                        # answer = generated[idxs_boa[0] + len(tkzer.boa_token):idxs_eot[-1]].lower()
                        else:
                            answer = answer_with_eos[:idxs_eot[0]].lower()  # Take the 1st one
                            id_pred = lb2id[answer]
                            if id_pred == -1:
                                assert all(lb.lower() != answer.lower() for lb in labels)  # sanity check
                    # ic(answer)
                    # ic(answer)
                    # Sanity check, no trivial mismatch
                    lbs_true = dset[i_sample]['labels']
                    # ic(lbs_true)
                    if id_pred in lbs_true:
                        id_true = next(lb_id for lb_id in lbs_true if lb_id == id_pred)
                    else:  # This renders class-level performance inaccurate; TODO?
                        id_true = lbs_true[0]  # Pick arbitrarily
                    # ic(id_pred, id_true)
                    # ic(preds.shape, trues.shape, i_sample)
                    preds[i_sample], trues[i_sample] = id_pred, id_true
                    return id_pred, id_true
                preds_batch, trues_batch = zip(*[
                    set_pred_n_true(out, i_sample) for out, i_sample in zip(outputs, idxs)
                ])
                d_log = dict(
                    step=f'{step+1:>{len(str(n_bch))}}/{n_bch}',
                    batch_size=f'{len(idxs):>{len(str(batch_size))}}/{batch_size}',
                    sequence_length=len(inputs['input_ids'][0]),
                    n_acc=sum(p == t for p, t in zip(preds_batch, trues_batch)),
                    ids_pred=list(preds_batch), ids_true=list(trues_batch)
                )
                logger.info(log_dict(d_log, with_color=True))
                logger_fl.info(log_dict(d_log, with_color=False))
                # ic(answers)
                # preds = [lb2id[a] for a in answers]
                # ic(lb2id)
                # ic(preds)
            df = pd.DataFrame(
                # note `-1` is not actual label, support of 0 - included for full label specification per sklearn
                # **note** cos the -1 label, the `macro avg` row is not accurate;
                # included it for getting global accuracy
                classification_report(
                    trues, preds, labels=[-1, *range(len(labels))], target_names=['Label not in dataset', *labels],
                    output_dict=True
                )
            ).transpose()
            path = os.path.join(path_dir, f'{dnm_}.csv')
            df.to_csv(path)
            logger.info(f'Evaluation on {logi(dnm_disk)} written to CSV at {logi(path)}')
            logger_fl.info(f'Evaluation on {dnm_disk} written to CSV at {path}')
            exit(1)


if __name__ == '__main__':
    from icecream import ic

    from zeroshot_encoder.util import *

    seed = config('random-seed')
    transformers.set_seed(seed)

    def train():
        # dnm = 'ag_news'
        dnm = 'UTCD'

        # nm = 'debug'
        # nm = 'debug-gpt-ori'
        # nm = 'debug-large'
        # nm = 'gpt2'
        nm = 'gpt2-medium'

        # n = 1
        n = 128
        # n = 1024
        # n = 4500
        # n = 1024 * 32
        # n = None

        tr_args = None
        # tr_args = dict(num_train_epochs=32)

        md, tkzer, dset_tr, dset_vl, trainer = get_all_setup(
            nm, dnm, do_eval=False, custom_logging=True, n_sample=n, random_seed=seed, train_args=tr_args
        )
        # TODO:
        eval_dataloader = trainer.get_eval_dataloader()
        # for step, inputs in enumerate(eval_dataloader):
        #     ic(inputs.keys())
        #     ic(inputs['input_ids'].device)
        #     exit(1)
        trainer.evaluate()
        exit(1)

        def profile_train():
            import cProfile
            import pstats
            profiler = cProfile.Profile()
            profiler.enable()
            trainer.train()
            profiler.disable()
            stats = pstats.Stats(profiler).sort_stats('cumtime')
            stats.print_stats()
        # profile_train()

        def train(resume=False):
            if resume:
                checkpoint_path = '/scratch/profmars_root/profmars0/stefanhg/Zero-shot-text-classification/' \
                                  'models/gpt2/gpt2-medium/2022-03-03 00-23-41/checkpoint-18533'
                trainer.train(checkpoint_path)  # Resume from checkpoint
            else:
                trainer.train()
            trainer.save_model(os.path.join(trainer.args.output_dir))
            # trainer.evaluate()
        # train(resume=True)

        def evaluate_trained():
            trainer.model = load_trained(epoch=3)  # Override the model
            ic(trainer.evaluate())
        # evaluate_trained()

        def profile_evaluate():
            trainer.model = load_trained(epoch=2)

            import cProfile
            import pstats
            profiler = cProfile.Profile()
            profiler.enable()

            ic(trainer.evaluate())

            profiler.disable()
            stats = pstats.Stats(profiler).sort_stats('cumtime')
            stats.print_stats()
        # profile_evaluate()

        def evaluate_ood():
            trainer.model = load_trained(epoch=2)

            dataset_name = 'UTCD-ood'
            # n_ = 1024
            # n_ = 1024 * 32
            n_ = None

            vl = get_dset(
                dataset_name=dataset_name,
                d_map_func=dict(test=tokenize_func(tkzer, dataset_name=dataset_name, mode='test')), splits='test',
                # Run on newly-added dset only
                filter_func=lambda sample: sample['dataset_id'] == config('UTCD.dataset_name2id')['multi_eurlex'],
                # **Note** no shuffling performed
                remove_columns=['label', 'text'], n_sample=n_
            )[0]
            # vl = vl.select(range(1024))  # TODO: debugging
            # gating with `if trainer.is_local_process_zero()`
            # somehow causes `torchrun` to not terminate after 1st compute loss
            ic(trainer.evaluate(eval_dataset=vl))
        evaluate_ood()
    # train()

    evaluate_trained(in_domain=False)
