import math
import os
import datetime
import configparser
from os.path import join as os_join
from typing import List, Tuple, Dict, Iterable, Optional

import numpy as np
import pandas as pd
import sklearn
from datasets import load_metric
import matplotlib.pyplot as plt

from stefutil import *
from zeroshot_classifier.util.data_path import BASE_PATH, PROJ_DIR, DSET_DIR, PKG_NM, MODEL_DIR


__all__ = [
    'sconfig', 'u', 'save_fig', 'plot_points',
    'on_great_lakes', 'get_base_path',
    'map_model_dir_nm', 'map_model_output_path', 'domain2eval_dir_nm', 'TrainStrategy2PairMap',
    'eval_res2df', 'compute_metrics'
]


sconfig = StefConfig(config_file=os_join(BASE_PATH, PROJ_DIR, PKG_NM, 'util', 'config.json')).__call__
u = StefUtil(
    base_path=BASE_PATH, project_dir=PROJ_DIR, package_name=PKG_NM, dataset_dir=DSET_DIR, model_dir=MODEL_DIR
)
u.plot_path = os_join(BASE_PATH, PROJ_DIR, 'plot')
save_fig = u.save_fig

for d in sconfig('check-arg'):
    ca.cache_mismatch(**d)


def plot_points(arr, **kwargs):
    """
    :param arr: Array of 2d points to plot
    :param kwargs: Arguments are forwarded to `matplotlib.axes.Axes.plot`
    """
    arr = np.asarray(arr)
    kwargs_ = dict(marker='.', lw=0.5, ms=1, c='orange')
    kwargs = {**kwargs_, **kwargs}  # python3.6 compatibility
    plt.plot(arr[:, 0], arr[:, 1], **kwargs)


def on_great_lakes():
    return 'arc-ts' in get_hostname()


def get_base_path():
    # For remote machines, save heavy-duty data somewhere else to save `/home` disk space
    hnm = get_hostname()
    if 'clarity' in hnm:  # Clarity lab
        return '/data'
    elif on_great_lakes():  # Great Lakes; `profmars0` picked arbitrarily among [`profmars0`, `profmars1`]
        # Per https://arc.umich.edu/greatlakes/user-guide/
        return os_join('/scratch', 'profmars_root', 'profmars0', 'stefanhg')
    else:
        return BASE_PATH


def config_parser2dict(conf: configparser.ConfigParser) -> Dict:
    return {sec: dict(conf[sec]) for sec in conf.sections()}


def map_model_dir_nm(
        model_name: str = None, name: str = None, mode: Optional[str] = 'vanilla',
        sampling: Optional[str] = 'rand', normalize_aspect: bool = False
) -> str:
    out = f'{now(for_path=True)}_{model_name}'
    if name:
        out = f'{out}-{name}'
    if mode:
        out = f'{out}-{mode}'
    if sampling:
        out = f'{out}-{sampling}'
    if normalize_aspect:
        out = f'{out}-aspect-norm'
    return out


def map_model_output_path(
        model_name: str = None, output_path: str = None, mode: Optional[str] = 'vanilla',
        sampling: Optional[str] = 'rand', normalize_aspect: bool = False
) -> str:
    def _map(dir_nm):
        return map_model_dir_nm(model_name, dir_nm, mode, sampling, normalize_aspect)
    if output_path:
        paths = output_path.split(os.sep)
        output_dir = _map(paths[-1])
        return os_join(*paths[:-1], output_dir)
    else:
        return os_join(get_base_path(), u.proj_dir, u.model_dir, _map(None))


def domain2eval_dir_nm(domain: str = 'in'):
    domain_str = 'in-domain' if domain == 'in' else 'out-of-domain'
    date = now(fmt='short-date')
    return f'{date}_{domain_str}'


class TrainStrategy2PairMap:
    sep_token = sconfig('training.implicit-on-text.encode-sep.aspect-sep-token')
    aspect2aspect_token = sconfig('training.implicit-on-text.encode-aspect.aspect2aspect-token')

    def __init__(self, train_strategy: str = 'vanilla'):
        self.train_strategy = train_strategy
        ca(training_strategy=train_strategy)

    def __call__(self, aspect: str = None):
        if self.train_strategy in ['vanilla', 'explicit']:
            def txt_n_lbs2query(txt: str, lbs: List[str]) -> List[List[str]]:
                return [[txt, lb] for lb in lbs]
        elif self.train_strategy == 'implicit':
            def txt_n_lbs2query(txt: str, lbs: List[str]) -> List[List[str]]:
                return [[txt, f'{lb} {aspect}'] for lb in lbs]
        elif self.train_strategy == 'implicit-on-text-encode-aspect':
            def txt_n_lbs2query(txt: str, lbs: List[str]) -> List[List[str]]:
                return [[f'{TrainStrategy2PairMap.aspect2aspect_token[aspect]} {txt}', lb] for lb in lbs]
        else:
            assert self.train_strategy == 'implicit-on-text-encode-sep'

            def txt_n_lbs2query(txt: str, lbs: List[str]) -> List[List[str]]:
                return [[f'{aspect} {TrainStrategy2PairMap.sep_token} {txt}', lb] for lb in lbs]
        return txt_n_lbs2query

    def map_label(self, label: str, aspect: str = None):
        if self.train_strategy == 'implicit':
            assert aspect is not None
            return f'{label} {aspect}'
        else:
            return label

    def map_text(self, text: str, aspect: str = None):
        if self.train_strategy in ['implicit-on-text-encode-aspect', 'implicit-on-text-encode-sep']:
            assert aspect is not None
            if self.train_strategy == 'implicit-on-text-encode-aspect':
                return f'{TrainStrategy2PairMap.aspect2aspect_token[aspect]} {text}'
            else:
                return f'{aspect} {TrainStrategy2PairMap.sep_token} {text}'
        else:
            return text


def eval_res2df(labels: Iterable, preds: Iterable, report_args: Dict = None, pretty: bool = True) -> Tuple[pd.DataFrame, float]:
    report = sklearn.metrics.classification_report(labels, preds, **(report_args or dict()))
    if 'accuracy' in report:
        acc = report['accuracy']
    else:
        vals = [v for k, v in report['micro avg'].items() if k != 'support']
        assert all(math.isclose(v, vals[0], abs_tol=1e-8) for v in vals)
        acc = vals[0]
    return pd.DataFrame(report).transpose(), round(acc, 3) if pretty else acc


def compute_metrics(eval_pred):
    if not hasattr(compute_metrics, 'acc'):
        compute_metrics.acc = load_metric('accuracy')
    logits, labels = eval_pred
    preds = np.argmax(logits, axis=-1)
    return dict(acc=compute_metrics.acc.compute(predictions=preds, references=labels)['accuracy'])


if __name__ == '__main__':
    from stefutil import *

    # mic(sconfig('fine-tune'))

    # mic(fmt_num(124439808))

    # process_utcd_dataset()

    # map_ag_news()

    def check_gl():
        mic(on_great_lakes())
        mic(get_base_path())
    check_gl()
