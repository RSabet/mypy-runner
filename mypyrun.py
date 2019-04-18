from __future__ import absolute_import, print_function

import argparse
import subprocess
import os
import sys
import re
import fnmatch

if sys.version_info[0] == 3:
    import configparser
else:
    import ConfigParser as configparser
from termcolor import colored, cprint

if False:
    from typing import *

# adapted from mypy:
CONFIG_FILE = 'mypyrun.ini'
SHARED_CONFIG_FILES = ('mypy.ini', 'setup.cfg')
USER_CONFIG_FILES = ('~/.mypy.ini',)
CONFIG_FILES = (CONFIG_FILE,) + SHARED_CONFIG_FILES + USER_CONFIG_FILES

# choose an exit that does not conflict with mypy's
PARSING_FAIL = 100

_FILTERS = [
    # DEFINITION ERRORS --
    # Type annotation errors:
    ('invalid_syntax', 'syntax error in type comment'),
    ('wrong_number_of_args', 'Type signature has '),
    ('misplaced_annotation', 'misplaced type annotation'),
    ('not_defined', ' is not defined'),  # in seminal
    ('invalid_type_arguments', '(".*" expects .* type argument)'  # in typeanal
                               '(Optional.* must have exactly one type argument)'
                               '(is not subscriptable)'),
    ('generator_expected', 'The return type of a generator function should be '),  # in messages
    # Advanced signature errors:
    ('orphaned_overload', 'Overloaded .* will never be matched'),  # in messages
    ('already_defined', 'already defined'),  # in seminal
    # Signature incompatible with function internals:
    ('return_expected', 'Return value expected'),
    ('return_not_expected', 'No return value expected'),
    ('incompatible_return', 'Incompatible return value type'),
    ('incompatible_yield', 'Incompatible types in "yield"'),
    ('incompatible_arg', 'Argument .* has incompatible type'),
    ('incompatible_default_arg', 'Incompatible default for argument'),
    # Signature/class incompatible with super class:
    ('incompatible_subclass_signature', 'Signature .* incompatible with supertype'),
    ('incompatible_subclass_return', 'Return type .* incompatible with supertype'),
    ('incompatible_subclass_arg', 'Argument .* incompatible with supertype'),
    ('incompatible_subclass_attr', 'Incompatible types in assignment '
                                   '\(expression has type ".*", base class '
                                   '".*" defined the type as ".*"\)'),

    # MISC --
    ('need_annotation', 'Need type annotation'),
    ('missing_module', 'Cannot find module '),

    # USAGE ERRORS --
    # Special case Optional/None issues:
    ('no_attr_none_case', 'Item "None" of ".*" has no attribute'),
    ('incompatible_subclass_attr_none_case',
     'Incompatible types in assignment \(expression has type ".*", base class '
     '".*" defined the type as "None"\)'),
    # Other:
    ('incompatible_list_comprehension', 'List comprehension has incompatible type'),
    ('cannot_assign_to_method', 'Cannot assign to a method'),
    ('not_enough_arguments', 'Too few arguments'),
    ('not_callable', ' not callable'),
    ('no_attr', '.* has no attribute'),
    ('not_indexable', ' not indexable'),
    ('invalid_index', 'Invalid index type'),
    ('not_iterable', ' not iterable'),
    ('not_assignable_by_index', 'Unsupported target for indexed assignment'),
    ('no_matching_overload', 'No overload variant of .* matches argument type'),
    ('incompatible_assignment', 'Incompatible types in assignment'),
    ('invalid_return_assignment', 'does not return a value'),
    ('unsupported_operand', 'Unsupported .*operand '),
    ('abc_with_abstract_attr', "Cannot instantiate abstract class .* with abstract attribute"),
]

FILTERS = [(n, re.compile(s)) for n, s in _FILTERS]
FILTERS_SET = frozenset(n for n, s in FILTERS)

COLORS = {
    'error': 'red',
    'warning': 'yellow',
    'note': None,
}

GLOBAL_ONLY_OPTIONS = ['color', 'show_ignored', 'show_error_keys']


class Options:
    """
    Options common to both the config file and the cli.

    Options like paths and mypy-options, which can be set via mypy are
    not recorded here.
    """
    select = frozenset()  # type: FrozenSet[str]
    ignore = frozenset()  # type: FrozenSet[str]
    warn = frozenset()  # type: FrozenSet[str]
    exclude = frozenset()  # type: FrozenSet[Pattern]
    paths = None  # type: List[str]
    color = True
    show_ignored = False
    show_error_keys = False


def get_error_code(msg):
    # type: (str) -> Optional[str]
    """
    Lookup the error constant from a parsed message literal.

    Parameters
    ----------
    msg : str

    Returns
    -------
    Optional[str]
    """
    for code, regex in FILTERS:
        if regex.search(msg):
            return code
    return None


def is_excluded_path(path, options):
    for regex in options.exclude:
        if regex.search(path):
            return True
    return False


def get_status(options, error_code):
    # type: (Options, str) -> Optional[str]
    """
    Determine whether an error code is an error, warning, or ignored

    Parameters
    ----------
    options: Options
    error_code: str

    Returns
    -------
    Optional[str]
    """
    if options.select:
        if error_code in options.select:
            return 'error'

    if options.warn:
        if error_code in options.warn:
            return 'warning'

    if options.ignore:
        if error_code in options.ignore:
            return None

    if options.ignore or not options.select:
        return 'error'

    return None


def report(options, filename, lineno, status, msg,
           is_filtered, error_key=None):
    # type: (Any, str, str, str, str, bool, Optional[str]) -> None
    """
    Report an error to stdout.

    Parameters
    ----------
    options : Options
    filename : str
    lineno : str
    status : str
    msg : str
    is_filtered : bool
    error_key : Optional[str]
    """
    if not options.color:
        if options.show_error_keys and error_key:
            msg = '%s: %s: %s' % (error_key, status, msg)
        else:
            msg = '%s: %s' % (status, msg)

        outline = 'IGNORED ' if options.show_ignored and is_filtered else ''
        outline += '%s:%s: %s' % (filename, lineno, msg)

    else:
        display_attrs = ['dark'] if options.show_ignored and is_filtered else None

        filename = colored(filename, 'cyan', attrs=display_attrs)
        lineno = colored(':%s: ' % lineno, attrs=display_attrs)
        color = COLORS[status]
        status = colored(status + ': ', color, attrs=display_attrs)
        if options.show_error_keys and error_key:
            status = colored(error_key + ': ', 'magenta',
                             attrs=display_attrs) + status
        msg = colored(msg, color, attrs=display_attrs)
        outline = filename + lineno + status + msg

    print(outline)


def run(mypy_options, options, daemon_mode=False):
    # type: (Optional[List[str]], Options, bool) -> int
    """
    Parameters
    ----------
    mypy_options : Optional[List[str]]
    options : Options
    daemon_mode : bool
        run `dmypy` instead of `mypy`

    Returns
    -------
    int
        exit status
    """
    if daemon_mode:
        args = ['dmypy', 'run', '--']
    else:
        args = ['mypy']

    if mypy_options:
        args.extend(mypy_options)

    if options.paths:
        env = os.environ.copy()
        mypy_path = env.get('MYPY_PATH')
        if mypy_path:
            mypy_path = os.pathsep.join([mypy_path] + options.paths)
        else:
            mypy_path = os.pathsep.join(options.paths)
        env['MYPY_PATH'] = mypy_path
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, env=env)
    else:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE)

    # used to know when to error a note related to an error
    matched_error = None
    errors = 0
    last_error = None  # type: Optional[Tuple[Options, Any, Any, Any, Optional[str]]]

    for line in proc.stdout:
        line = line.decode()
        try:
            filename, lineno, status, msg = line.split(':', 3)
        except ValueError:
            lineno = ''
            try:
                filename, status, msg = line.split(':', 2)
            except ValueError:
                print(line, end='')
                continue

        if is_excluded_path(filename, options):
            continue

        error_code = get_error_code(msg)
        status = status.strip()
        msg = msg.strip()

        last_error = options, filename, lineno, msg, error_code

        if error_code and status == 'error':
            new_status = get_status(options, error_code)
            if new_status == 'error':
                errors += 1
            if options.show_ignored or new_status:
                report(options, filename, lineno, new_status or 'error',
                       msg, not new_status, error_code)
                matched_error = new_status, error_code
            else:
                matched_error = None
        elif status == 'note' and matched_error is not None:
            report(options, filename, lineno, status, msg,
                   not matched_error[0], matched_error[1])

    returncode = proc.wait()
    if returncode != 1:
        # severe error: print everything that wasn't formatted as a standard
        # error
        cprint("Warning: A severe error occurred", "red")
        if last_error:
            options, filename, lineno, msg, error_code = last_error
            report(options, filename, lineno, 'error', msg, False)

    return returncode if errors else 0


def main():
    options = Options()
    parser = get_parser()

    error_codes = get_error_codes()

    args = parser.parse_args()
    if args.list:
        for name in sorted(error_codes):
            print('  %s' % (name,))
        sys.exit(0)

    parsers = [
        ConfigFileOptionsParser(),
        ArgparseOptionsParser(parser, args)
    ]

    for p in parsers:
        p.apply(options)

    if args.select_all:
        options.select = set(error_codes)
        options.show_ignored = True

    # if options.select:
    #     options.select.add('invalid_syntax')

    overlap = options.select.intersection(options.ignore)
    if overlap:
        print('The same option must not be both selected and '
              'ignored: %s' % ', '.join(overlap), file=sys.stderr)
        sys.exit(PARSING_FAIL)

    _validate(options.select, error_codes)
    _validate(options.ignore, error_codes)
    _validate(options.warn, error_codes)

    unused = set(error_codes).difference(options.ignore)
    unused = unused.difference(options.select)
    _validate(unused, error_codes)

    sys.exit(run(args.flags, options, args.daemon))


# Options Handling

def _parse_multi_options(options, split_token=','):
    # type: (str, str) -> List[str]
    r"""
    Split and strip and discard empties.

    Turns the following:

    >>> _parse_multi_options("    A,\n    B,\n")
    ["A", "B"]

    Parameters
    ----------
    options : str
    split_token : str

    Returns
    -------
    List[str]
    """
    if options:
        return [o.strip() for o in options.split(split_token) if o.strip()]
    else:
        return []


def _validate(filters, error_codes):
    # type: (Set[str], Set[str]) -> None
    """
    Parameters
    ----------
    filters : Set[str]
    error_codes : Set[str]
    """
    invalid = sorted(filters.difference(error_codes))
    if invalid:
        print('Invalid filter(s): %s\n' % ', '.join(invalid), file=sys.stderr)
        sys.exit(PARSING_FAIL)


config_types = {
    'select': lambda x: set(_parse_multi_options(x)),
    'ignore': lambda x: set(_parse_multi_options(x)),
    'warn': lambda x: set(_parse_multi_options(x)),
    'paths': lambda x: list(_parse_multi_options(x)),
    'exclude': lambda x: [re.compile(fnmatch.translate(x))
                          for x in _parse_multi_options(x)]
}


class BaseOptionsParser:
    def extract_updates(self, options):
        # type: (Options) -> Iterator[Tuple[Dict[str, object], Optional[str]]]
        raise NotImplementedError

    def apply(self, options):
        for updates, fpath in self.extract_updates(options):
            if updates:
                for k, v in updates.items():
                    setattr(options, k, v)


class ConfigFileOptionsParser(BaseOptionsParser):
    def __init__(self, filename=None):
        self.filename = filename

    def _parse_section(self, prefix, template, section):
        # type: (str, Options, configparser.SectionProxy) -> Dict[str, object]
        """
        Parameters
        ----------
        prefix : str
        template : Options
        section : configparser.SectionProxy

        Returns
        -------
        Dict[str, object]
        """
        results = {}  # type: Dict[str, object]
        for key in section:
            if key in config_types:
                ct = config_types[key]
            else:
                dv = getattr(template, key, None)
                if dv is None:
                    print("%s: Unrecognized option: %s = %s" % (prefix, key, section[key]),
                          file=sys.stderr)
                    continue
                ct = type(dv)
            v = None  # type: Any
            try:
                if ct is bool:
                    v = section.getboolean(key)  # type: ignore  # Until better stub
                elif callable(ct):
                    try:
                        v = ct(section.get(key))
                    except argparse.ArgumentTypeError as err:
                        print("%s: %s: %s" % (prefix, key, err), file=sys.stderr)
                        continue
                else:
                    print("%s: Don't know what type %s should have" % (prefix, key), file=sys.stderr)
                    continue
            except ValueError as err:
                print("%s: %s: %s" % (prefix, key, err), file=sys.stderr)
                continue
            results[key] = v
        return results

    def extract_updates(self, options):
        # type: (Options) -> Iterator[Tuple[Dict[str, object], Optional[str]]]
        if self.filename is not None:
            config_files = (self.filename,)  # type: Tuple[str, ...]
        else:
            config_files = tuple(map(os.path.expanduser, CONFIG_FILES))

        parser = configparser.RawConfigParser()

        for config_file in config_files:
            if not os.path.exists(config_file):
                continue
            try:
                parser.read(config_file)
            except configparser.Error as err:
                print("%s: %s" % (config_file, err), file=sys.stderr)
            else:
                file_read = config_file
                # options.config_file = file_read
                break
        else:
            print("No config files found")
            return

        if 'mypyrun' not in parser:
            if self.filename or file_read not in SHARED_CONFIG_FILES:
                print("%s: No [mypyrun] section in config file" % file_read,
                      file=sys.stderr)
        else:
            section = parser['mypyrun']

            prefix = '%s: [%s]' % (file_read, 'mypy')
            yield self._parse_section(prefix, options, section), None

        for name, section in parser.items():
            if name.startswith('mypyrun-'):
                prefix = '%s: [%s]' % (file_read, name)
                updates = self._parse_section(prefix, options, section)

                if set(updates).intersection(GLOBAL_ONLY_OPTIONS):
                    print("%s: Per-module sections should only specify per-module flags (%s)" %
                          (prefix, ', '.join(sorted(set(updates).intersection(GLOBAL_ONLY_OPTIONS)))),
                          file=sys.stderr)
                    updates = {k: v for k, v in updates.items() if k in Options.PER_MODULE_OPTIONS}
                globs = name[5:]
                for glob in globs.split(','):
                    yield updates, glob


class ArgparseOptionsParser(BaseOptionsParser):
    def __init__(self, parser, parsed):
        self.parser = parser
        self.parsed = parsed

    def _get_specified(self):
        # type: () -> Dict[str, object]
        parsed_kwargs = dict(self.parsed._get_kwargs())
        specified = {}  # type: Dict[str, object]
        for action in self.parser._get_optional_actions():
            if action.dest in parsed_kwargs:
                if parsed_kwargs[action.dest] != action.default:
                    specified[action.dest] = parsed_kwargs[action.dest]
        return specified

    def extract_updates(self, options):
        # type: (Options) -> Iterator[Tuple[Dict[str, object], Optional[str]]]
        results = {}  # type: Dict[str, object]
        for key, v in self._get_specified().items():
            if key in config_types:
                ct = config_types[key]
                try:
                    v = ct(v)
                except argparse.ArgumentTypeError as err:
                    print("%s: %s" % (key, err), file=sys.stderr)
                    continue
            else:
                dv = getattr(options, key, None)
                if dv is None:
                    continue
            results[key] = v
        yield results, None


def get_error_codes():
    # type: () -> FrozenSet[str]
    """
    Returns
    -------
    FrozenSet[str]
    """
    return FILTERS_SET


def get_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list",
                        help="list error codes",
                        action="store_true")
    parser.add_argument("--daemon", "-d",
                        help="run in daemon mode (dmypy run)",
                        action="store_true")
    parser.add_argument("--select", "-s",
                        help="Errors to check (comma separated)")
    parser.add_argument("--ignore",  "-i",
                        help="Errors to skip (comma separated)")
    parser.add_argument("--no-color", dest="color",
                        default=True,
                        help="do not colorize output",
                        action="store_false")
    parser.add_argument("--show-ignored", "-x",
                        help="Show errors that have been ignored (darker"
                             " if using color)",
                        action="store_true")
    parser.add_argument("--show-error-keys",
                        help="Show error key for each line",
                        action="store_true")
    parser.add_argument("--select-all",
                        help="Enable all selections (for debugging missing choices)",
                        action="store_true")
    parser.add_argument('flags', metavar='ARG', nargs='*', type=str,
                        help="Regular mypy flags and files (precede with --)")
    return parser


if __name__ == '__main__':
    main()
