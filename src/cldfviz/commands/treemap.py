"""
Plot values for a parameter of the dataset against a Glottolog family tree.

Tree specification:

- value labels and order
- config file?

Make this work with
- metadata-free datasets (i.e. values.csv only, metadata from Glottolog)

"""
import io
import logging
import pathlib
import webbrowser
import collections

import newick
from pyglottolog.objects import Glottocode
from pycldf.cli_util import add_dataset, get_dataset
from pycldf.ext import discovery
from pycldf.trees import TreeTable

from cldfviz.cli_util import add_testable
from cldfviz.glottolog import Glottolog

try:
    import pandas as pd
    from Bio import Phylo
    import yaml
    import lingtreemaps
except ImportError:  # pragma: no cover
    lingtreemaps = None


class DF:
    def __init__(self):
        self.df = None
        self.acc = collections.defaultdict(list)

    def __enter__(self):
        return self

    def add(self, item):
        if self.df is None:
            self.df = pd.DataFrame(columns=list(item))
        for k, v in item.items():
            self.acc[k].append(v)

    def __exit__(self, exc_type, exc_val, exc_tb):
        for k, v in self.acc.items():
            self.df[k] = v


def df_from_dicts(dicts):
    with DF() as df:
        for d in dicts:
            df.add(d)
    return df.df


def yaml_type(s):
    return yaml.load(io.StringIO(s), yaml.SafeLoader)


def iter_ltm_options():
    h = []
    if lingtreemaps:
        for line in pathlib.Path(lingtreemaps.__file__).parent\
                .joinpath('data', 'default_config.yaml').read_text(encoding='utf8').split('\n'):
            line = line.strip()
            if line.startswith('#'):
                h.append(line[1:].strip())
            elif line:
                opt, _, default = line.partition(':')
                opt, val, help = opt.strip(), yaml_type(default.strip()), ' '.join(h)
                if opt == 'filename':
                    help = "The filename. If unspecified, the parameter ID will be used."
                yield opt, val, help
                h = []


def register(parser):
    add_testable(parser)
    add_dataset(parser)
    Glottolog.add(parser)
    parser.add_argument('parameter')
    parser.add_argument(
        '--tree',
        help="Tree specified as Glottocode (interpreted as the root of the Glottolog tree), "
             "Newick formatted string or path to a file containing the Newick formatted "
             "tree.")
    parser.add_argument(
        '--tree-dataset', default=None,
    )
    parser.add_argument(
        '--tree-id', default=None,
    )
    parser.add_argument(
        '--glottocodes-as-tree-labels',
        action='store_true',
        default=False,
        help="If a tree in a TreeTable of a CLDF dataset is used, the nodes will be renamed "
             "using the corresponding Glottocodes."
    )
    parser.add_argument(
        '--tree-label-property',
        help="Name of the language property used to identify languages in the tree.",
        default='glottocode')
    for opt, default, help in iter_ltm_options():
        parser.add_argument(
            '--ltm-{}'.format(opt.replace('_', '-')),
            default=default,
            type=yaml_type,
            help=help)


def run(args):
    if lingtreemaps is None:  # pragma: no cover
        args.log.error(
            'install cldfviz with lingtreemaps, running "pip install cldfviz[lingtreemaps]"')
        return

    assert args.tree or args.tree_dataset
    glottolog = Glottolog.from_args(args)

    ds = get_dataset(args)

    # 1. Get all values for the selected parameter:
    cols = ['parameterReference', 'languageReference', 'value']
    if ds.get(('ValueTable', 'codeReference')):
        cols.append('codeReference')
    values = {
        v['languageReference']: v for v in ds.iter_rows('ValueTable', *cols)
        if v['parameterReference'] == args.parameter}

    # 2. Get the tree ...
    if args.tree:
        if Glottocode.pattern.match(args.tree):
            tree = glottolog.newick(args.tree)
        elif pathlib.Path(args.tree).exists():
            tree = newick.read(args.tree)[0]
        else:
            tree = newick.loads(args.tree)[0]
    else:
        treeds = discovery.get_dataset(args.tree_dataset, args.download_dir)
        for tree in TreeTable(treeds):
            if (args.tree_id and tree.id == args.tree_id) or \
                    ((not args.tree_id) and tree.tree_type == 'summary'):
                tree = tree.newick()
                break
        else:
            raise ValueError('No matching tree found')  # pragma: no cover
        # Rename tree nodes:
        if args.glottocodes_as_tree_labels:
            name_map = {
                r['id']: r['glottocode']
                for r in treeds.iter_rows('LanguageTable', 'id', 'glottocode')}

            def rename(n):
                n.name = name_map.get(n.name)
                return n
            tree.visit(rename)

    # ... and its set of node labels.
    nodes = {n.name for n in tree.walk()}

    # 3. Get the set of matching languages in the dataset.
    treelabel2id = {}  # Maps tree label to language ID for languages in both, dataset and tree.
    languages = {}  # A dict of language-data dicts
    if 'LanguageTable' in ds:
        for lang in ds.iter_rows(
                'LanguageTable', 'id', 'glottocode', 'name', 'latitude', 'longitude'):
            if lang['id'] in values:
                # Determine which tree node name corresponds to the language:
                treelabel = lang[args.tree_label_property]
                if treelabel and treelabel in nodes:  # The language is in the tree.
                    languages[lang['id']] = lang
                    treelabel2id[treelabel] = lang['id']
    else:
        assert glottolog
        for lid in values:
            if lid in nodes:
                glang = glottolog[lid]
                languages[lid] = dict(name=lid, latitude=glang.lat, longitude=glang.lon)
                treelabel2id[lid] = lid

    for lid in list(values.keys()):
        if lid not in languages:
            del values[lid]

    # Now all values correspond to languages which are in the tree!
    assert set(values) == set(languages)

    # 4. Now we prune the tree to contain just leafs for which we have data.
    keep = {n.name for n in tree.walk() if n.name in treelabel2id}
    tree.prune_by_names(list(keep), inverse=True)
    if sum(1 for _ in tree.walk()) == 1:  # pragma: no cover
        raise ValueError('No overlap between dataset and tree')

    # 5. Rename the tree nodes
    def rename(n):
        if n.is_leaf:
            # FIXME: only quote if necessary!?
            n.name = "'{}'".format(languages[treelabel2id[n.name]]['name'])
        else:
            n.name = None
        return n
    tree.visit(rename)

    leafs = {n.unquoted_name for n in tree.walk() if n.is_leaf}
    assert leafs.issubset(set(languages[v]['name'] for v in values))
    # lingtreemaps cannot handle the case where we have values for both a language and a dialect
    # of this language. Thus, we only keep data for actual leafs.
    nonleafs = []
    for lid, lang in languages.items():
        if lang['name'] not in leafs:
            nonleafs.append(lid)
    for lid in nonleafs:
        del languages[lid]
        del values[lid]

    # 6. Turn languages and values into the appropriate data structures for lingtreemaps:
    if 'CodeTable' in ds:
        codes = collections.OrderedDict([
            (c['id'], c['name'])
            for c in ds.iter_rows('CodeTable', 'id', 'name', 'parameterReference')
            if c['parameterReference'] == args.parameter])
    else:  # pragma: no cover
        codes = None

    # Sort values by value/code. ltm will just use this order.
    values = sorted(
        values.values(), key=lambda v: v['value'] if not codes else codes[v['codeReference']])
    values = [dict(Clade=languages[v['languageReference']]['name'],
                   Value=v['value'] if not codes else codes[v['codeReference']]) for v in values]

    languages = [dict(ID=lg['name'], Latitude=lg['latitude'], Longitude=lg['longitude'])
                 for lg in languages.values()]

    languages = df_from_dicts(languages)
    values = df_from_dicts(values)

    kwargs = {k.replace('ltm_', ''): v for k, v in args.__dict__.items() if k.startswith('ltm_')}
    kwargs['filename'] = kwargs['filename'] or args.parameter
    fname = pathlib.Path(
        kwargs['filename'] if '.' in kwargs['filename']
        else '{filename}.{file_format}'.format(**kwargs))

    logging.getLogger('matplotlib.font_manager').setLevel(logging.ERROR)
    lingtreemaps.plot(languages, Phylo.read(io.StringIO(tree.newick), 'newick'), values, **kwargs)

    args.log.info('Output written to: {}'.format(fname))
    if args.test:
        return
    webbrowser.open(fname.resolve().as_uri())  # pragma: no cover
