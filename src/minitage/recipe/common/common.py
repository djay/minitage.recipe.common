# Copyright (C) 2009, Mathieu PASQUET <kiorky@cryptelium.net>
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
# 3. Neither the name of the <ORGANIZATION> nor the names of its
#    contributors may be used to endorse or promote products derived from
#    this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.



__docformat__ = 'restructuredtext en'

import copy
import imp
import logging
import os
import re
import shutil
import sys
try:
    from hashlib import md5 as mmd5
except Exception, e:
    from md5 import new as mmd5
import subprocess
import urlparse
from distutils.dir_util import copy_tree

from minitage.core.common import get_from_cache, system, splitstrip
from minitage.core.unpackers.interfaces import IUnpackerFactory
from minitage.core.fetchers.interfaces import IFetcherFactory
from minitage.core import core

__logger__ = 'minitage.recipe'

RESPACER = re.compile('\s\s*', re.I|re.M|re.U|re.S).sub

def uniquify(l):
    result = []
    for i in l:
        if not i in result:
            result.append(i)
    return result

def divide_url(url):
    scmargs_sep = '|'
    url_parts = url.split(scmargs_sep)
    surl, type, revision, directory, scmargs = '', '', '', '', ''
    if url_parts:
        surl = url_parts[0].strip()
    if len(url_parts) > 1:
        type = url_parts[1].strip()
    if len(url_parts) > 2:
        revision = url_parts[2].strip()
    if len(url_parts) > 3:
        directory = url_parts[3].strip()
    if not(directory) and (surl) and ('//' in surl):
        directory = surl.replace('://', '/').replace('/', '.')
    if len(url_parts) > 4:
        scmargs = scmargs_sep.join(url_parts[4:]).strip()
    if 'file://' in url:
        directory = os.path.basename('')
    surl = surl and surl or ''
    type = type and type or 'static'
    revision = revision and revision or ''
    directory = directory and directory or ''
    scmargs = scmargs and scmargs or ''
    return surl, type, revision, directory, scmargs

def appendVar(var, values, separator='', before=False):
    oldvar = copy.deepcopy(var)
    tmp = separator.join([value for value in values if not value in var])
    if before:
        if not tmp:
            separator = ''
        var = "%s%s%s" % (oldvar, separator,  tmp)
    else:
        if not oldvar:
            separator = ''
        var = "%s%s%s" % (tmp, separator, oldvar)
    return var

def which(program, environ=None, key = 'PATH', split = ':'):
    if not environ:
        environ = os.environ
    PATH=environ.get(key, '').split(split)
    for entry in PATH:
        fp = os.path.abspath(os.path.join(entry, program))
        if os.path.exists(fp):
            return fp
    raise IOError('Program not fond: %s in %s ' % (program, PATH))


class MinitageCommonRecipe(object):
    """
    Downloads and installs a distutils Python distribution.
    """
    def __init__(self, buildout, name, options):
        """__init__.
        The code is voulantary not splitted
        because i want to use this object as an api for the other
        recipes.
        """
        self.logger = logging.getLogger(__logger__)
        self.buildout, self.name, self.options = buildout, name, options
        self.offline = buildout.offline
        self.install_from_cache = self.options.get('install-from-cache', None)

        # url from and scm type if any
        # the scm is one available in the 'fetchers' factory
        self.url = self.options.get('url', None)
        self.urls_list = self.options.get('urls', '').strip().split('\n')
        self.urls_list.insert(0,self.url)
        # remove trailing /
        self.urls_list = [re.sub('/$', '', u) for u in self.urls_list if u]
        self.default_scm = self.options.get('scm', 'static')
        self.default_scm_revision = self.options.get('revision', '')
        self.default_scm_args = self.options.get('scm-args', '')

        # construct a dict in the form:
        # with that, it keeps compatibilty with older recipes.
        # { url {type:'', args: ''}}
        self.urls = {}
        for kurl in self.urls_list:
            # ignore double checkouted urls in all cases
            if not kurl in self.urls:
                url, scmtype, scmrevison, scmdirectory, scmargs = divide_url(kurl)
                if not scmargs:
                    scmargs = self.default_scm_args
                if not scmrevison:
                    scmrevison = self.default_scm_revision
                if not scmtype:
                    scmtype = self.default_scm
                self.urls[url] = {
                    'url' : url,
                    'type': scmtype,
                    'args': scmargs,
                    'revision': scmrevison,
                    'directory': scmdirectory,
                }

        # If 'download-cache' has not been specified,
        # fallback to [buildout]['downloads']
        buildout['buildout'].setdefault(
            'download-cache',
            buildout['buildout'].get(
                'download-cache',
                os.path.join(
                    buildout['buildout']['directory'],
                    'downloads'
                )
            )
        )
        # update with the desired env. options
        if 'environment' in options:
            if not '=' in options["environment"]:
                lenv = buildout.get(options['environment'].strip(), {})
                for key in lenv:
                    os.environ[key] = lenv[key]
            else:
                 for line in options["environment"].split("\n"):
                     try:
                         lparts = line.split('=')
                         key = lparts[0]
                         value = '='.join(lparts[1:])
                         key, _, value = line.partition('=')
                         os.environ[key] = value
                     except Exception, e:
                         pass

        # maybe md5
        self.md5 = self.options.get('md5sum', None)

        # system variables
        self.uname = sys.platform
        if 'linux' in self.uname:
            # linuxXXX ?
            self.uname = 'linux'
        self.cwd = os.getcwd()
        self.minitage_directory = os.path.abspath(
            os.path.join(self.buildout['buildout']['directory'], '..', '..')
        )

        # destination
        options['location'] = os.path.abspath(options.get('location',
                                          os.path.join(
                                              buildout['buildout']['parts-directory'],
                                              options.get('name', self.name)
                                          )
                                         ))
        self.prefix = self.options.get('prefix', options['location'])
        if options.get("shared", "false").lower() == "true":
            pass

        # if we are installing in minitage, try to get the
        # minibuild name and object there.
        self.str_minibuild = os.path.split(self.cwd)[1]
        self.minitage_section = {}

        # system/libraries dependencies
        self.minitage_dependencies = []
        # distutils python stuff
        self.minitage_eggs = []

        self._skip_flags = self.options.get('skip-flags', False)

        # compilation flags
        self.includes = splitstrip(self.options.get('includes', ''))
        self.cflags = RESPACER(
            ' ',
            self.options.get( 'cflags', '').replace('\n', '')
        ).strip()
        self.ldflags = RESPACER(
            ' ',
            self.options.get( 'ldflags', '').replace('\n', '')
        ).strip()
        self.includes += splitstrip(self.options.get('includes-dirs', ''))
        self.libraries = splitstrip(self.options.get('library-dirs', ''))
        self.libraries_names = ' '
        for l in self.options.get('libraries', '').split():
            self.libraries_names += '-l%s ' % l
        self.rpath = splitstrip(self.options.get('rpath', ''))

        # separate archives in downloaddir/minitage
        self.download_cache = os.path.join(
            buildout['buildout']['directory'],
            buildout['buildout'].get('download-cache'),
            'minitage'
        )

        # do we install cextension stuff
        self.build_ext = self.options.get('build-ext', '')

        # patches stuff
        self.patch_cmd = self.options.get(
            'patch-binary',
            'patch'
        ).strip()

        self.patch_options = ' '.join(
            self.options.get(
                'patch-options', '-Np0'
            ).split()
        )
        self.patches = self.options.get('patches', '').split()
        if 'patch' in self.options:
            self.patches.append(
                self.options.get('patch').strip()
            )
        # conditionnaly add OS specifics patches.
        self.patches.extend(
            splitstrip(
                self.options.get(
                    '%s-patches' % (self.uname.lower()),
                    ''
                )
            )
        )
        if 'freebsd' in self.uname.lower():
            self.patches.extend(
                splitstrip(
                    self.options.get(
                        'freebsd-patches',
                        ''
                    )
                )
            )

        # path we will put in env. at build time
        self.path = splitstrip(self.options.get('path', ''))

        # pkgconfigpath
        self.pkgconfigpath = splitstrip(self.options.get('pkgconfigpath', ''))

        # python path
        self.pypath = [self.buildout['buildout']['directory'],
                       self.options['location']]
        self.pypath.extend(self.pypath)
        self.pypath.extend(
            splitstrip(self.options.get('pythonpath', ''))
        )

        # tmp dir
        self.tmp_directory = os.path.join(
            buildout['buildout'].get('directory'),
            '__minitage__%s__tmp' % name
        )

        # minitage specific
        # we will search for (priority order)
        # * [part : minitage-dependencies/minitage-eggs]
        # * a [minitage : deps/eggs]
        # * the minibuild dependencies key.
        # to get the needed dependencies and put their
        # CFLAGS / LDFLAGS / RPATH / PYTHONPATH / PKGCONFIGPATH
        # into the env.
        if 'minitage' in buildout:
            self.minitage_section = buildout['minitage']

        self.minitage_section['dependencies'] = '%s %s' % (
                self.options.get('minitage-dependencies', ' '),
                self.minitage_section.get('dependencies', ' '),
        )

        self.minitage_section['eggs'] = '%s %s' % (
                self.options.get('minitage-eggs', ' '),
                self.minitage_section.get('eggs', ' '),
        )

        # try to get dependencies from the minibuild
        #  * add them to deps if dependencies
        #  * add them to eggs if they are eggs
        # but be non bloquant in errors.
        self.minibuild = None
        self.minimerge = None
        minibuild_dependencies = []
        minibuild_eggs = []
        self.minitage_config = os.path.join(
            self.minitage_directory, 'etc', 'minimerge.cfg')
        try:
            self.minimerge = core.Minimerge({
                'nolog' : True,
                'config': self.minitage_config
                }
            )
        except:
            message = 'Problem when intiializing minimerge '\
                    'instance with %s config.'
            self.logger.debug(message % self.minitage_config)

        try:
            self.minibuild = self.minimerge._find_minibuild(
                self.str_minibuild
            )
        except:
            message = 'Problem looking for \'%s\' minibuild'
            self.logger.debug(message % self.str_minibuild)

        if self.minibuild:
            for dep in self.minibuild.dependencies :
                m = None
                try:
                    m = self.minimerge._find_minibuild(dep)
                except Exception, e:
                    message = 'Problem looking for \'%s\' minibuild'
                    self.logger.debug(message % self.str_minibuild)
                if m:
                    if m.category == 'eggs':
                        minibuild_eggs.append(dep)
                    if m.category == 'dependencies':
                        minibuild_dependencies.append(dep)

        self.minitage_dependencies.extend(
            [os.path.abspath(os.path.join(
                self.minitage_directory,
                'dependencies', s, 'parts', 'part'
            )) for s in splitstrip(
                self.minitage_section.get('dependencies', '')
            ) + minibuild_dependencies ]
        )

        # sometime we install system libraries as eggs because they depend on
        # a particular python version !
        # There is there we suceed in getting their libraries/include into
        # enviroenmment by dealing with them along with classical
        # system dependencies.
        for s in self.minitage_dependencies + self.minitage_eggs:
            self.includes.append(os.path.join(s, 'include'))
            self.libraries.append(os.path.join(s, 'lib'))
            self.rpath.append(os.path.join(s, 'lib'))
            self.pkgconfigpath.append(os.path.join(s, 'lib', 'pkgconfig'))
            self.path.append(os.path.join(s, 'bin'))
            self.path.append(os.path.join(s, 'sbin'))

        # Defining the python interpreter used to install python stuff.
        # using the one defined in the key 'executable'
        # fallback to sys.executable or
        # python-2.4 if self.name = site-packages-2.4
        # python-2.5 if self.name = site-packages-2.5
        self.executable= None
        if 'executable' in options:
            for lsep in '.', '..':
                if lsep in options['executable']:
                    self.executable = os.path.abspath(options.get('executable').strip())
                else:
                    self.executable = options.get('executable').strip()
        elif 'python' in options:
            self.executable = self.buildout.get(
                options['python'].strip(),
                {}).get('executable', None)
        elif 'python' in self.buildout:
            self.executable = self.buildout.get(
                self.buildout['buildout']['python'].strip(),
                {}).get('executable', None)
        if not self.executable:
            # if we are an python package
            # just get the right interpreter for us.
            # and add ourselves to the deps
            # to get the cflags/ldflags in env.
            for pyver in core.PYTHON_VERSIONS:
                if self.name == 'site-packages-%s' % pyver:
                    interpreter_path = os.path.join(
                        self.minitage_directory,
                        'dependencies', 'python-%s' % pyver, 'parts',
                        'part'
                    )
                    self.executable = os.path.join(
                        interpreter_path, 'bin', 'python'
                    )
                    self.minitage_dependencies.append(interpreter_path)
                    self.includes.append(
                        os.path.join(
                            interpreter_path,
                            'include',
                            'python%s' % pyver
                        )
                    )
        # If we have not python selected yet, default to the current one
        if not self.executable:
            self.executable = self.buildout.get(
                buildout.get('buildout', {}).get('python', '').strip(), {}
                ).get('executable', sys.executable)

        # if there is no '/' in the executalbe, just search for in the path
        if not self.executable.startswith('/'):
            self._set_path()
            try:
                self.executable = which(self.executable)
            except IOError, e:
                raise core.MinimergeError('Python executable '
                                 'was not found !!!\n\n%s' % e)

        # which python version are we using ?
        self.executable_version = os.popen(
            '%s -c "%s"' % (
                self.executable ,
                'import sys;print sys.version[:3]'
            )
        ).read().replace('\n', '')

        # where is the python installed, we need it to filter later
        # wrong site-packages picked up by setuptools envrionments scans
        try:
            self.executable_prefix = os.path.abspath(
                    subprocess.Popen(
                        [self.executable, '-c', 'import sys;print sys.prefix'],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        close_fds=True).stdout.read().replace('\n', '')
                )
        except:
            # getting the path from the link, if we can:
            try:
                if not self.executable.endswith('/'):
                    executable_directory = self.executable.split('/')[:-1]
                    if executable_directory[-1] in ['bin', 'sbin']:
                        level = -1
                    else:
                        level = None
                    self.executable_prefix = '/'.join(executable_directory[:level])
                else:
                    raise core.MinimergeError('Your python executable seems to point to a directory!!!')
            except:
                raise

        if not os.path.isdir(self.executable_prefix):
            message = 'Python seems not to find its prefix : %s' % self.executable_prefix
            self.logger.warning(message)

        self.executable_lib = os.path.join(
                        self.executable_prefix,
                        'lib', 'python%s' % self.executable_version)

        self.executable_site_packages = os.path.join(
                        self.executable_prefix,
                        'lib', 'python%s' % self.executable_version,
                        'site-packages')

        # site-packages defaults
        self.site_packages = 'site-packages-%s' % self.executable_version
        self.site_packages_path = self.options.get(
            'site-packages',
            os.path.join(
                self.buildout['buildout']['directory'],
                'parts',
                self.site_packages)
        )

        self.minitage_eggs.extend(
            [os.path.abspath(os.path.join(
                self.minitage_directory,
                'eggs', s, 'parts', self.site_packages,
            )) for s in splitstrip(
                self.minitage_section.get('eggs', '')
            ) + minibuild_eggs]
        )

        for s in self.minitage_eggs \
                 + [self.site_packages_path] \
                 + [self.buildout['buildout']['eggs-directory']] :
            self.pypath.append(s)

        # cleaning if we have a prior compilation step.
        if os.path.isdir(self.tmp_directory):
            self.logger.info(
                'Removing pre existing '
                'temporay directory: %s' % (
                    self.tmp_directory)
            )
            shutil.rmtree(self.tmp_directory)

    def _download(self,
                  url=None,
                  destination=None,
                  scm=None,
                  revision=None,
                  scm_args=None,
                  cache=None,
                  md5 = None,
                  use_cache = True):
        """Download the archive.
        url: url to download
        destination: where to download (directory)
        scm: scm to use if any
        revision: revision to checkout if any
        cache: cache in a subdirectory, either  SCM/urldirectory or urlmd5sum/filename
        md5: file md5sum
        use_cache : if False, always redownload even if the file is there
        """
        self.logger.info('Download archive')
        if not url:
            url = self.url

        if not url:
            raise core.MinimergeError('URL was not set!')

        if not destination and (url in self.urls):
            d = self.urls[url]['directory']
            if d:
                if os.path.sep in d:
                    destination = os.path.abspath(d)
        if not destination:
            destination = self.download_cache

        if not scm:
            if url in self.urls:
                scm = self.urls[url]['type'].strip()
        if (not scm) or os.path.exists(url):
            scm = 'static'

        # we use a special function for static files as the generic static
        # fetcher do some magic for md5 uand unpacking and are unwanted there?
        if scm != 'static':
            if cache is None:
                cache = False
            # if we have a fetcher in minibuild dependencies, we make it come in
            # the PATH:
            self._set_path()
            opts = {}

            if not revision:
                # compatibility
                if not (url in self.urls):
                    revision = self.default_scm_revision
                else:
                    r = self.urls[url]['revision'].strip()
                    if r:
                        revision = r

            if not scm_args:
                # compatibility
                if not (url in self.urls):
                    scm_args = self.default_scm_args
                else:
                    a  = self.urls[url]['args'].strip()
                    if a:
                        scm_args = a

            if scm_args:
                opts['args'] = scm_args

            if revision:
                opts['revision'] = revision

            scm_dest = destination
            if cache:
                scm_dir = os.path.join(
                    destination, scm)
                if not os.path.isdir(scm_dir):
                    os.makedirs(scm_dir)
                subdir = url.replace('://', '/').replace('/', '.')
                scm_dest = os.path.join(scm_dir, subdir)

            # fetching now
            if not self.offline:
                ff = IFetcherFactory(self.minitage_config)
                scm = ff(scm)
                scm.fetch_or_update(scm_dest, url, opts)
            else:
                if not os.path.exists(scm_dest):
                    message = 'Can\'t get a working copy from \'%s\''\
                              ' into \'%s\' when we are in offline mode'
                    raise core.MinimergeError(message % (url, scm_dest))
                else:
                    self.logger.info('We assumed that \'%s\' is the result'
                                     ' of a check out as'
                                     ' we are running in'
                                     ' offline mode.' % scm_dest
                                    )
            return scm_dest
        else:
            if cache:
                m = mmd5()
                m.update(url)
                url_md5sum = m.hexdigest()
                _, _, urlpath, _, fragment = urlparse.urlsplit(url)
                filename = urlpath.split('/')[-1]
                destination = os.path.join(destination, "%s_%s" % (filename, url_md5sum))
            if destination and not os.path.isdir(destination):
                os.makedirs(destination)
            return get_from_cache(
                url = url,
                download_cache = destination,
                logger = self.logger,
                file_md5 = md5,
                offline = self.offline,
                use_cache=use_cache
            )

    def _set_py_path(self, ws=None):
        """Set python path.
        Arguments:
            - ws : setuptools WorkingSet
        """
        self.logger.info('Setting path')
        # setuptool ws maybe?
        if ws:
            self.pypath.extend(ws.entries)
        # filter out site-packages not relevant to our python installation
        remove_last_slash = re.compile('\/$').sub
        pypath = []
        for entry in [remove_last_slash('', e) for e in self.pypath]:
            sp = (self.executable_site_packages,
                  os.path.join('lib', 'python%s' % self.executable_version,
                  'site-packages')
            )
            lib = (self.executable_lib,
                   os.path.join('lib', 'python%s' % self.executable_version)
            )
            for path, atom in (sp, lib):
                add = True
                if entry.endswith(atom) and not path == entry:
                    add = False
            if add :
                pypath.append(entry)
        # uniquify the list
        pypath = uniquify(pypath)
        os.environ['PYTHONPATH'] = ':'.join(pypath)


    def _set_path(self):
        """Set path."""
        self.logger.info('Setting path')
        os.environ['PATH'] = appendVar(os.environ['PATH'],
                     uniquify(self.path)\
                     + [self.buildout['buildout']['directory'],
                        self.options['location']]\
                     , ':')


    def _set_pkgconfigpath(self):
        """Set PKG-CONFIG-PATH."""
        self.logger.info('Setting pkgconfigpath')
        pkgp = os.environ.get('PKG_CONFIG_PATH', '').split(':')
        os.environ['PKG_CONFIG_PATH'] = ':'.join(
            uniquify(self.pkgconfigpath+pkgp)
        )

    def _set_compilation_flags(self):
        """Set CFALGS/LDFLAGS."""
        self.logger.info('Setting compilation flags')
        if self._skip_flags:
            self.logger.warning('!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!')
            self.logger.warning('!!! Build variable settings has been disabled !!!')
            self.logger.warning('!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!')
            return
        os.environ['CFLAGS']  = ' '.join([os.environ.get('CFLAGS', ''),
                                          '  %s' % self.cflags]).strip()
        os.environ['LDFLAGS']  = ' '.join([os.environ.get('LDFLAGS', '')
                                           , '  %s' % self.ldflags]).strip()
        if self.rpath:
            os.environ['LD_RUN_PATH'] = appendVar(
                os.environ.get('LD_RUN_PATH', ''),
                [s for s in self.rpath\
                 if s.strip()]
                + [os.path.join(self.prefix, 'lib')],
                ':'
            )

        if self.libraries:
            darwin_ldflags = ''
            if self.uname == 'darwin':
                # need to cut backward comatibility in the linker
                # to get the new rpath feature present
                # >= osx Leopard
                darwin_ldflags = ' -mmacosx-version-min=10.5.0 '

            os.environ['LDFLAGS'] = appendVar(
                os.environ.get('LDFLAGS',''),
                ['-L%s -Wl,-rpath -Wl,%s' % (s,s) \
                 for s in self.libraries + [os.path.join(self.prefix, 'lib')]
                 if s.strip()]
                + [darwin_ldflags] ,
                ' '
            )

            if self.uname == 'cygwin':
                os.environ['LDFLAGS'] = ' '.join(
                    [os.environ['LDFLAGS'],
                     '-L/usr/lib -L/lib -Wl,-rpath -Wl,/usr/lib -Wl,-rpath -Wl,/lib']
                )
        if self.libraries_names:
            os.environ['LDFLAGS'] = ' '.join([os.environ.get('LDFLAGS', ''), self.libraries_names]).strip()

        if self.minimerge:
            os.environ['CFLAGS']  = ' '.join([
                os.environ.get('CFLAGS', ' '),
                ' ',
                self.minimerge._config._sections.get('minitage.compiler', {}).get('cflags', ''),
                ' ']
            )
            os.environ['LDFLAGS']  = ' '.join([
                os.environ.get('LDFLAGS', ' '),
                ' ',
                self.minimerge._config._sections.get('minitage.compiler', {}).get('ldflags', ''),
                ' ']
            )
            os.environ['MAKEOPTS']  = ' '.join([
                os.environ.get('MAKEOPTS', ' '),
                ' ',
                self.minimerge._config._sections.get('minitage.compiler', {}).get('makeopts', ''),
                ' ']
            )
        if self.includes:
            os.environ['CFLAGS'] = appendVar(
                os.environ.get('CFLAGS', ''),
                ['-I%s' % s \
                 for s in self.includes\
                 if s.strip()]
                ,' '
            )
            os.environ['CPPFLAGS'] = appendVar(
                os.environ.get('CPPFLAGS', ''),
                ['-I%s' % s \
                 for s in self.includes\
                 if s.strip()]
                ,' '
            )
            os.environ['CXXFLAGS'] = appendVar(
                os.environ.get('CXXFLAGS', ''),
                ['-I%s' % s \
                 for s in self.includes\
                 if s.strip()]
                ,' '
            )
        for key in ('CFLAGS', 'CPPFLAGS', 'CXXFLAGS', 'LDFLAGS', 'LD_RUN_PATH'):
            os.environ[key] = RESPACER(' ', os.environ.get(key, '')).strip()

    def _unpack(self, fname, directory=None):
        """Unpack something"""
        if not directory:
            directory = self.tmp_directory
        self.logger.info('Unpacking in %s.' % directory)
        if os.path.isdir(fname):
            if not os.path.exists(directory):
                os.makedirs(directory)
            copy_tree(fname, directory)
        else:
            unpack_f = IUnpackerFactory()
            u = unpack_f(fname)
            u.unpack(fname, directory)

    def _patch(self, directory, patch_cmd=None,
               patch_options=None, patches =None, download_dir=None):
        """Aplying patches in pwd directory."""
        if not patch_cmd:
            patch_cmd = self.patch_cmd
        if not patch_options:
            patch_options = self.patch_options
        if not patches:
            patches = self.patches
        if patches:
            self.logger.info('Applying patches.')
            cwd = os.getcwd()
            os.chdir(directory)
            for patch in patches:
                # check the md5 of the patch to see if it is the same.
                fpatch = self._download(patch,
                                        destination=download_dir,
                                        md5=None,
                                        cache=True,
                                        use_cache = False,
                                       )
                system('%s -t %s < %s' %
                       (patch_cmd,
                        patch_options,
                        fpatch),
                       self.logger
                      )
                os.chdir(cwd)

    def update(self):
        pass

    def _call_hook(self, hook, destination=None):
        """
        This method is copied from z3c.recipe.runscript.
        See http://pypi.python.org/pypi/z3c.recipe.runscript for details.
        """
        cwd = os.getcwd()
        hooked = False
        if destination:
            os.chdir(destination)

        if hook in self.options \
           and len(self.options[hook].strip()) > 0:
            hooked = True
            self.logger.info('Executing %s' % hook)
            script = self.options[hook]
            filename, callable = script.split(':')
            filename = os.path.abspath(filename)
            module = imp.load_source('script', filename)
            getattr(module, callable.strip())(
                self.options, self.buildout
            )

        if destination:
            os.chdir(cwd)
        return hooked


    def _system(self, cmd):
        """Running a command."""
        self.logger.info('Running %s' % cmd)
        self.go_inner_dir()
        p = subprocess.Popen(cmd, env=os.environ, shell=True)
        ret = 0
        try:
            sts = os.waitpid(p.pid, 0)
            ret = sts [1]
        except:
            ret = 1
        # ret = os.system(cmd)
        if ret:
            raise  core.MinimergeError('Command failed: %s' % cmd)



# vim:set et sts=4 ts=4 tw=80:
