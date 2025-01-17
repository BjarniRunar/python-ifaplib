# This file is part of Mailfile
#
# Mailfile is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as
# published by the Free Software Foundation, either version 3 of
# the License, or (at your option) any later version.
#
# Mailfile is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with Mailfile. If not, see <https://www.gnu.org/licenses/>.
#
"""\
Command Line Interface for interacting with Mailfile filesystems.

Run `python -m mailfile help` for instructions.
"""
import base64
import getopt
import getpass
import hashlib
import imaplib
import json
import os
import sys

from . import Mailfile
from .backends import FilesystemIMAP


def _fail(msg, code=1):
    sys.stderr.write(msg+'\n')
    sys.exit(code)


def _loginfile():
    return os.path.expanduser('~/.mailfile-login')


def _load_creds():
    try:
        creds = {}
        with open(_loginfile(), 'r') as fd:
            creds.update(json.loads(base64.b64decode(fd.read())))
        return creds
    except (OSError, IOError, ValueError):
        return None


def _get_mailfile(creds=None):
    if creds is None:
        creds = _load_creds()
        if creds is None:
            _fail('Please log in first.', code=2)

    host, port = creds['imap'].split(':')
    if host == 'maildir':
        imap = FilesystemIMAP(port, create=0o700)
    else:
        port = int(port)
        while not creds.get('password'):
            creds['password'] = getpass.getpass(
                'IMAP password for %(username)s@%(imap)s: ' % creds).strip()
        try:
            cls = (imaplib.IMAP4 if (port == 143) else imaplib.IMAP4_SSL)
            imap = cls(host, port)
            imap.login(creds['username'], creds['password'])
        except cls.error as e:
            _fail('IMAP login failed: %s' % e, code=3)

    mailfile = Mailfile(imap, creds['mailbox'])
    if creds['key'] and creds['key'] != 'None':
        mailfile.set_encryption_key(creds['key'])
    return mailfile


def _clean_path(path):
    while path[:1] == '/':
        path = path[1:]
    while path[-1:] == '/':
        path = path[:-1]
    return path.replace('//', '/')


def _put_command(opts, args):
    """Put a file or files in Mailfile (upload)

Example: python -m mailfile put README.md setup.py /tmp
Options:
    -v, --verbose     Report progress on stdout

The last argument should be the destination directory."""
# FIXME:  -r, --recurse     Upload entire directory trees
    dest = _clean_path(args.pop(-1))
    opts = dict(opts)
    for fn in args:
        if not os.path.exists(fn):
            raise OSError("File not found: %s" % fn)
    if not args:
        return True
    with _get_mailfile() as mailfile:
        for fn in args:
            if dest:
                dest_fn = os.path.join(dest, os.path.basename(fn))
            else:
                dest_fn = os.path.basename(fn)
            with open(fn, 'r') as fd:
                data = fd.read()
            with mailfile.open(dest_fn, 'w') as fd:
                fd.write(data)
            if '--verbose' in opts or '-v' in opts:
                print("%s -> mailfile:%s" % (fn, dest_fn))
    return True


def _get_command(opts, args):
    """Fetch a file or files from Mailfile (download)

This command will fetch its arguments from Mailfile and store as local
files. The name of the created files will be derived in the obvious
way from the name in Mailfile.

Example: python -m mailfile get /tmp/README.md /tmp/README.txt .
Options:
    -v, --verbose     Report progress on stdout
    -r, --recurse     Fetch entire directory trees
    -f, --force       Overwrite already existing files, if necessary
    --version=N       Request a specific versions of the files

The last argument should be the destination directory. When requesting a
specific version, it doesn't make sense to request multiple files."""
    dest_dir = args.pop(-1)
    if not os.path.exists(dest_dir) or not os.path.isdir(dest_dir):
         _fail('Not a directory: %s' % dest_dir)
    mailfile = _get_mailfile()

    full_path = False
    def _fn(fn):
        while fn[:1] == '/':
            fn = fn[1:]
        if full_path:
            target = os.path.join(dest_dir, fn)
        else:
            target = os.path.join(dest_dir, os.path.basename(fn))
        return target

    def _pmkdir(fn):
        dn = os.path.dirname(fn)
        if not os.path.exists(dn):
            if dn and fn != dn:
                _pmkdir(dn)
                os.mkdir(dn)

    opts = dict(opts)
    if '--recurse' in opts or '-r' in opts:
        full_path = True
        files = []
        with mailfile:
            ls = sorted(mailfile._tree.keys())
        for prefix in args:
            while prefix[:1] == '/':
                prefix = prefix[1:]
            files.extend([f for f in ls if f.startswith(prefix)])
        args = sorted(list(set(files)))

    if '--force' not in opts and '-f' not in opts:
        for fn in args:
            target = _fn(fn)
            if os.path.exists(target):
                _fail('Cravenly refusing to overwrite %s' % target)

    version = int(opts.get('-V', opts.get('--version', 0))) or None
    if version and len(args) > 1:
        _fail('Multiple files and --version are incompatible.')
    with mailfile:
        for fn in args:
            while fn[:1] == '/':
                fn = fn[1:]
            target = _fn(fn)
            if full_path:
                _pmkdir(target)
            data = mailfile.open(fn, 'r', version=version).read()
            open(target, 'w').write(data)
            if '--verbose' in opts or '-v' in opts:
                print("mailfile:%s -> %s" % (fn, target))
    return True


def _cat_command(opts, args):
    """Print the contents of a file or files from Mailfile

Example: python -m mailfile cat /tmp/README.md
Options:
    --version=N       Request a specific versions of the file

When requesting a specific version, it doesn't make sense to request
multiple files."""
    opts = dict(opts)
    version = int(opts.get('-V', opts.get('--version', 0))) or None
    if version and len(args) > 1:
        _fail('Multiple files and --version are incompatible.')
    with _get_mailfile() as mailfile:
        for fn in args:
            while fn[:1] == '/':
                fn = fn[1:]
            with mailfile.open(fn, 'r', version=version) as fd:
                sys.stdout.write(fd.read())
    return True


def _vers_command(opts, args):
    """Set the desired number of versions for a file

Example: python -m mailfile vers 4 /tmp/README.md

"""
    opts = dict(opts)
    versions = int(args.pop(0))
    with _get_mailfile() as mailfile:
        for fn in args:
            with mailfile.open(fn, 'r+') as fd:
                fd.metadata['versions'] = versions
        mailfile.synchronize(snapshot=True)
    return True


def _rm_command(opts, args):
    """Remove a file or files

Example: python -m mailfile rm /tmp/README.md
Options:
    --version=N       Remove a specific versions of the file

Note: removing the deletion marker will undelete the file!
"""
    opts = dict(opts)
    version = int(opts.get('-V', opts.get('--version', 0)))
    if version and len(args) != 1:
        _fail('Multiple files and --version are incompatible.')
    with _get_mailfile() as mailfile:
        for fn in args:
            mailfile.remove(fn, versions=([version] if version else None))
        mailfile.synchronize(cleanup=True, snapshot=True)
    return True


def _ls_command(opts, args):
    """List files

Example: python -m mailfile ls -l /
Options:
    -l, --metadata     List full metadata for each file
    -a, --all          List all files

Defaults to listing the root directory, if any arguments are present it
will list those directories instead."""
    opts = dict(opts)

    verbose = ('-l' in opts or '--long' in opts or '--metadata' in opts)
    def _ls(mailfile, files):
        if verbose:
            ll = max(len(f) for f in files)
            fmt = '%%-%d.%ds %%s' % (ll, ll)
            for f in files:
                if f in ('.', '..'):
                    continue
                if f in mailfile._tree:
                    print(fmt % (f, json.dumps({
                        'metadata': mailfile._tree[f][1],
                        'versions': sorted(list(mailfile._tree[f][2]))},
                        sort_keys=True)))
                else:
                    print(fmt % (f, '{}'))
        else:
            print('\n'.join(files))

    with _get_mailfile() as mailfile:
        if '-a' in opts or '--all' in opts:
            flist = sorted(mailfile._tree.keys())
        elif not args:
            flist = mailfile.listdir('/')
        else:
            flist = []
            for prefix in args:
                flist.extend(mailfile.listdir(prefix))
        if flist:
            _ls(mailfile, sorted(list(set(flist))))
    return True


def _mount_command(opts, args):
    """Mount an Mailfile filesystem using FUSE

Example: python -m mailfile mount ./tmp
Options:
    -v, --verbose     Log activity to STDERR.

The process will hang, you can put it in the background yourself if you
prefer."""
    try:
        from .fuse_driver import mount
    except ImportError as e:
        _fail('Is fusepy installed? Error: %s' % e, 98)
    opts = dict(opts)
    verbose = ('-v' in opts or '--verbose' in opts)
    mount(_get_mailfile(), args[0], verbose=verbose)
    return True


def _logout_command(opts, args):
    """Log out from an IMAP/Mailfile server

This will delete your IMAP password from ~/.mailfile-login.  Note that it
will leave the secret key and other settings intact, remove the file by
hand if you want them gone too.
"""
    creds = _load_creds()
    del creds['password']
    with open(_loginfile(), 'w') as fd:
        os.chmod(_loginfile(), 0o600)
        fd.write(base64.encodestring(json.dumps(creds)))
    sys.stderr.write('OK: Deleted password from %s\n' % _loginfile())
    return True


def _login_command(opts, args):
    """Log in to an IMAP/Mailfile server

Options:
    --imap=host:port       Defaults to "localhost:143"
    --mailbox=mailbox      Defaults to "FILE_STORAGE"
    --username=username    Defaults to $USER
    --password=password    Defaults to prompting the user
    --key=random_string    Defaults to generating a new, strong key

If the key is set to the string "None" (without the quotes), that
will disable Mailfile's encryption.

Setting the IMAP server to maildir:/path/to/folder will use the
built-in local Maildir storage, instead of real IMAP.

Warning: This will store your IMAP and Mailfile access credentials,
lightly obfuscated, in ~/.mailfile-login. Use the logout command to
delete the IMAP password from this file."""
    defaults = _load_creds() or {}
    opts = dict(opts)
    creds = {
        'imap': opts.get('--imap', defaults.get('imap', 'localhost:143')),
        'mailbox': opts.get('--mailbox', defaults.get('mailbox', 'FILE_STORAGE')),
        'username': opts.get('--username', defaults.get('username', os.getenv('USER'))),
        'password': opts.get('--password', defaults.get('password')),
        'key': opts.get('--key', defaults.get('key'))}
    if not creds['imap'].startswith('maildir:'):
        while not creds['password']:
            creds['password'] = getpass.getpass(
                'IMAP password for %(username)s@%(imap)s: ' % creds).strip()
    if creds['key'] is None:
        creds['key'] = base64.b64encode(os.urandom(32)).strip()
        sys.stderr.write('Generated key: %s\n' % creds['key'])

    _get_mailfile(creds).synchronize()

    with open(_loginfile(), 'w') as fd:
        os.chmod(_loginfile(), 0o600)
        fd.write(base64.encodestring(json.dumps(creds)))
    return True


def _help_command(opts, args):
    """Get help

You can get further instructions on each command by running
`help command`."""
    for cmd in args:
        print('%s: %s' % (cmd, dict(_COMMANDS)[cmd][0].__doc__))
    if not args:
        print("""\
This is the Command Line Interface for Mailfile filesystems

Usage: python -m mailfile <command> [options] [arguments...]
Commands:

%(commands)s

Examples:
    python -m mailfile help login
    python -m mailfile cat /project/README.md
    python -m mailfile ls -l
""" % {'commands': '\n'.join([
            '    %-10.10s %s' % (cmd, synopsis[0].__doc__.splitlines()[0])
            for cmd, synopsis in _COMMANDS])})
    return True


_COMMANDS = [
    ('help',   (_help_command,   '',      [])),
    ('ls',     (_ls_command,     'al',    ['all', 'long', 'metadata'])),
    ('put',    (_put_command,    'vr',    ['verbose', 'recurse'])),
    ('get',    (_get_command,    'vrfV:', ['verbose', 'recurse', 'force',
                                           'version='])),
    ('cat',    (_cat_command,    'V:',    ['version='])),
    ('rm',     (_rm_command,     'V:',    ['version='])),
    ('vers',   (_vers_command,   '',      [])),
    ('mount',  (_mount_command,  'v',     ['verbose'])),
    ('login',  (_login_command,  '',      ['imap=', 'username=', 'mailbox=',
                                           'password=', '--key='])),
    ('logout', (_logout_command, '',     []))]


def cli():
    try:
        cmd, shortlist, longlist = dict(_COMMANDS)[sys.argv[1]]
        if not cmd(*getopt.getopt(sys.argv[2:], shortlist, longlist)):
            sys.exit(1)
    except KeyboardInterrupt:
        sys.stderr.write('Interrupted\n')
        sys.exit(99)
    except (getopt.GetoptError, IndexError) as e:
        _help_command([], [])
        if len(sys.argv) > 1:
            sys.stderr.write('Error(%s): %s\n' % (sys.argv[1], e))
        sys.exit(1)
