#!/usr/bin/env python
# coding: utf-8
"""
Payu: A generic driver for numerical models on the NCI computing clusters
-------------------------------------------------------------------------------
Contact: Marshall Ward <marshall.ward@anu.edu.au>
-------------------------------------------------------------------------------
Distributed as part of Payu, Copyright 2011 Marshall Ward
Licensed under the Apache License, Version 2.0
http://www.apache.org/licenses/LICENSE-2.0
"""

# Standard Library
import errno
import grp
import getpass
import os
import pwd
import resource
import sys
import shlex
import shutil as sh
import subprocess as sp

# Extensions
import yaml

# Local
from fs import mkdir_p, make_symlink

# Environment module support on vayu
execfile('/opt/Modules/default/init/python')
module_path = '/projects/v45/modules'
core_modules = ['python', 'payu']

# Default payu parameters
default_archive_url = 'dc.nci.org.au'
default_config_fname = 'config.yaml'

#==============================================================================
class Experiment(object):

    #---
    def __init__(self, **kwargs):

        # Disable group write access and all public access
        perms = 0o0027
        os.umask(perms)

        # TODO: __init__ should not be a config dumping ground!

        # TODO: Move to run/collate/sweep?
        self.read_config()
        self.set_pbs_config()
        self.set_model_pathnames()
        self.set_run_pathnames()
        self.set_counters()

        self.set_input_paths()
        self.set_output_paths()

        stacksize = self.config.get('stacksize')
        if stacksize:
            self.set_stacksize(stacksize)

        self.postscript = self.config.get('postprocess')


    #---
    def read_config(self):
        # TODO: Parse the PAYU_CONFIGPATH envar
        config_fname = default_config_fname

        try:
            with open(config_fname, 'r') as config_file:
                self.config = yaml.load(config_file)
        except IOError as ec:
            if ec.errno == errno.ENOENT:
                self.config = {}
            else:
                raise


    #---
    def set_counters(self):
        # Assume that ``set_paths`` has already been called
        assert self.archive_path

        current_counter = os.environ.get('PAYU_CURRENT_RUN')
        if current_counter:
            self.counter = int(current_counter)
        else:
            self.counter = None

        self.n_runs = int(os.environ.get('PAYU_N_RUNS', 1))

        # Initialize counter if unset
        if self.counter is None:
            # TODO: this logic can probably be streamlined
            try:
                restart_dirs = [d for d in os.listdir(self.archive_path)
                                if d.startswith('restart')]
            except OSError as ec:
                if ec.errno == errno.ENOENT:
                    restart_dirs = None
                else:
                    raise

            if restart_dirs:
                self.counter = 1 + max([int(d.lstrip('restart'))
                                        for d in restart_dirs
                                        if d.startswith('restart')])
            else:
                self.counter = 0


    #---
    def set_stacksize(self, stacksize):

        if stacksize == 'unlimited':
            stacksize = resource.RLIM_INFINITY
        else:
            # TODO: User-friendly explanation
            assert type(stacksize) is int

        resource.setrlimit(resource.RLIMIT_STACK,
                           (stacksize, resource.RLIM_INFINITY))


    #---
    def load_modules(self):
        # TODO: ``reversion`` makes a lot of this redundant

        # Unload non-essential modules
        loaded_mods = os.environ.get('LOADEDMODULES', '').split(':')

        for mod in loaded_mods:
            mod_base = mod.split('/')[0]
            if not mod_base in core_modules:
                module('unload', mod)

        # Now load model-dependent modules
        for mod in self.modules:
            module('load', mod)

        # TODO: Improved ipm support
        if 'ipm' in self.modules:
            os.environ['IPM_LOGDIR'] = self.work_path


    #---
    def set_pbs_config(self):

        default_n_cpus = os.environ.get('PBS_NCPUS', 1)
        self.n_cpus = self.config.get('ncpus', default_n_cpus)

        self.n_cpus_per_node = self.config.get('npernode')

        default_job_name = os.path.basename(os.getcwd())
        self.job_name = self.config.get('jobname', default_job_name)

        # Set group identifier for output
        # TODO: Do we even use this anymore? It's too slow
        #       Use the qsub flag?
        self.archive_group = self.config.pop('archive_group', None)


    #---
    def set_model_pathnames(self):

        # Local "control" path

        default_control_path = os.getcwd()
        self.control_path = self.config.get('control', default_control_path)

        # Top-level "laboratory" path

        default_short_path = os.path.join('/short', os.environ.get('PROJECT'))
        self.short_path = self.config.get('shortpath', default_short_path)

        default_user = pwd.getpwuid(os.getuid()).pw_name
        self.user_name = self.config.get('user', default_user)

        assert self.model_name
        default_lab_path = os.path.join(self.short_path, self.user_name,
                                        self.model_name)
        self.lab_path = self.config.get('laboratory', default_lab_path)

        # Executable path ("bin")
        self.bin_path = os.path.join(self.lab_path, 'bin')


    #---
    def set_run_pathnames(self):

        # Experiment name
        assert self.control_path
        default_experiment = os.path.basename(self.control_path)
        self.experiment = self.config.get('experiment', default_experiment)

        # Experiment subdirectories
        assert self.lab_path
        self.archive_path = os.path.join(self.lab_path, 'archive',
                                         self.experiment)
        self.work_path = os.path.join(self.lab_path, 'work', self.experiment)

        # Symbolic paths to output
        self.work_sym_path = os.path.join(self.control_path, 'work')
        self.archive_sym_path = os.path.join(self.control_path, 'archive')

        # Executable path
        assert self.bin_path
        assert self.default_exec
        assert self.model_name
        exec_name = self.config.pop('exe', self.default_exec)
        self.exec_path = os.path.join(self.bin_path, exec_name)

        # Stream output filenames
        self.stdout_fname = self.model_name + '.out'
        self.stderr_fname = self.model_name + '.err'


    #---
    def set_input_paths(self):
        # TODO: Allow multiple input paths, and move this into a "link_input"
        #       function (or something similar)

        input_dir = self.config.get('input')
        if input_dir:
            # Test for absolute path
            if os.path.exists(input_dir):
                self.input_path = input_dir
            else:
                # Test for path relative to /${lab_path}/input
                rel_path = os.path.join(self.lab_path, 'input', input_dir)
                if os.path.exists(rel_path):
                    self.input_path = rel_path
                else:
                    sys.exit('Input data not found; aborting.')
        else:
            self.input_path = None


    #---
    def set_output_paths(self):
        # Local archive paths
        output_dir = 'output{:03}'.format(self.counter)
        self.output_path = os.path.join(self.archive_path, output_dir)

        # TODO: check case counter == 0
        prior_output_dir = 'output{:03}'.format(self.counter - 1)
        prior_output_path = os.path.join(self.archive_path, prior_output_dir)
        if os.path.exists(prior_output_path):
            self.prior_output_path = prior_output_path
        else:
            self.prior_output_path = None

        # Local restart paths
        res_dir = 'restart{:03}'.format(self.counter)
        self.res_path = os.path.join(self.archive_path, res_dir)

        prior_res_dir = 'restart{:03}'.format(self.counter - 1)
        prior_res_path = os.path.join(self.archive_path, prior_res_dir)
        if os.path.exists(prior_res_path):
            self.prior_res_path = prior_res_path
        else:
            self.prior_res_path = None
            if self.counter > 0:
                # TODO: This warning should be replaced with an abort in setup
                print 'Warning: no restart files found.'


    #---
    def setup(self, do_stripe=False):

        # Confirm that no output path already exists
        if os.path.exists(self.output_path):
            sys.exit('Archived path already exists; aborting.')

        mkdir_p(self.work_path)

        # Stripe directory in Lustre
        if do_stripe:
            cmd = 'lfs setstripe -c 8 -s 8m {0}'.format(self.work_path).split()
            rc = sp.Popen(cmd).wait()
            assert rc == 0

        make_symlink(self.work_path, self.work_sym_path)

        for f in self.config_files:
            f_path = os.path.join(self.control_path, f)
            sh.copy(f_path, self.work_path)


    #---
    def run(self, *mpi_flags):
        f_out = open(self.stdout_fname, 'w')
        f_err = open(self.stderr_fname, 'w')

        # Use explicit path to avoid wrappers (if found)
        mpi_basedir = os.environ.get('OMPI_ROOT')
        if mpi_basedir:
            mpirun_cmd = os.path.join(mpi_basedir, 'bin', 'mpirun')
        else:
            mpirun_cmd = 'mpirun'

        # TODO: Rewrite to use ' '.join() or append to mpi_flags
        if self.n_cpus:
            mpirun_cmd += ' -np {0}'.format(self.n_cpus)

        if self.n_cpus_per_node:
            mpirun_cmd += ' -npernode {0}'.format(self.n_cpus_per_node)

        cmd = '{mpi} {flags} {bin}'.format(
                    mpi = mpirun_cmd,
                    flags = ' '.join(mpi_flags),
                    bin = self.exec_path)

        cmd = shlex.split(cmd)
        rc = sp.call(cmd, stdout=f_out, stderr=f_err)
        f_out.close()
        f_err.close()

        # Remove any empty output files (e.g. logs)
        for fname in os.listdir(self.work_path):
            fpath = os.path.join(self.work_path, fname)
            if os.path.getsize(fpath) == 0:
                os.remove(fpath)

        # TODO: Need a model-specific cleanup method call here
        if rc != 0:
            sys.exit('Error {0}; aborting.'.format(rc))

        # Decrement run counter on successful run
        self.n_runs -= 1

        # Move logs to archive (or delete if empty)
        for f in (self.stdout_fname, self.stderr_fname):
            if os.path.getsize(f) == 0:
                os.remove(f)
            else:
                sh.move(f, self.work_path)


    #---
    def archive(self, collate=True):
        mkdir_p(self.archive_path)

        make_symlink(self.archive_path, self.archive_sym_path)

        # Remove work symlink
        if os.path.islink(self.work_sym_path):
            os.remove(self.work_sym_path)

        # Double-check that the run path does not exist
        if os.path.exists(self.output_path):
            sys.exit('Archived path already exists; aborting.')

        cmd = 'mv {src} {dst}'.format(src=self.work_path, dst=self.output_path)
        rc = sp.Popen(cmd.split()).wait()
        assert rc == 0

        if self.archive_group:
            self.regroup()

        if collate:
            cmd = 'payu collate -i {0}'.format(self.counter)

            cmd = shlex.split(cmd)
            rc = sp.Popen(cmd).wait()
            assert rc == 0


    #---
    def postprocess(self):
        """Submit a postprocessing script after collation"""
        assert self.postscript

        cmd = 'qsub {}'.format(self.postscript)

        cmd = shlex.split(cmd)
        rc = sp.call(cmd)
        assert rc == 0, 'Postprocessing script submission failed.'


    #---
    def remote_archive(self, config_name, archive_url=None,
                       max_rsync_attempts=1, rsync_protocol=None):

        if not archive_url:
            archive_url = default_archive_url

        archive_address = '{usr}@{url}'.format(usr=getpass.getuser(),
                                               url=archive_url)

        ssh_key_path = os.path.join(os.getenv('HOME'), '.ssh',
                                    'id_rsa_file_transfer')

        # Top-level path is implicitly set by the SSH key
        # (Usually /projects/[group])

        # Remote mkdir is currently not possible, so any new subdirectories
        # must be created before auto-archival

        remote_path = os.path.join(self.model_name, config_name, self.name)
        remote_url = '{addr}:{path}'.format(addr=archive_address,
                                            path=remote_path)

        # Rsync ouput and restart files
        rsync_cmd = 'rsync -a --safe-links -e "ssh -i {key}" '.format(
                        key=ssh_key_path)

        if rsync_protocol:
            rsync_cmd += '--protocol={p} '.format(p=rsync_protocol)

        run_cmd = rsync_cmd + '{src} {dst}'.format(src=self.output_path,
                                                   dst=remote_url)
        rsync_calls = [run_cmd]

        if (self.counter % 5) == 0 and os.path.isdir(self.res_path):
            # Tar restart files before rsyncing
            res_tar_path = self.res_path + '.tar.gz'

            cmd = 'tar -C {path} -czf {fpath} {res}'.format(
                        path=self.archive_path,
                        fpath=res_tar_path,
                        res=os.path.basename(self.res_path)
                        ).split()
            rc = sp.Popen(cmd).wait()

            restart_cmd = rsync_cmd + '{src} {dst}'.format(src=res_tar_path,
                                                           dst=remote_url)
            rsync_calls.append(restart_cmd)
        else:
            res_tar_path = None

        if self.input_path and os.path.isdir(self.input_path):
            # Using explicit path separators to rename the input directory
            input_cmd = rsync_cmd + '{src} {dst}'.format(
                            src=self.input_path + os.sep,
                            dst=os.path.join(remote_url, 'input') + os.sep)
            rsync_calls.append(input_cmd)

        for cmd in rsync_calls:
            cmd = shlex.split(cmd)

            for rsync_attempt in range(max_rsync_attempts):
                rc = sp.Popen(cmd).wait()
                if rc == 0:
                    break
                else:
                    print 'rsync failed, reattempting'
            assert rc == 0

        # TODO: Temporary; this should be integrated with the rsync call
        if res_tar_path and os.path.exists(res_tar_path):
            os.remove(res_tar_path)


    #---
    def regroup(self):
        uid = os.getuid()
        gid = grp.getgrnam(self.archive_group).gr_gid

        os.lchown(self.archive_path, uid, gid)
        for root, dirs, files in os.walk(self.archive_path):
            for d in dirs:
                os.lchown(os.path.join(root, d), uid, gid)
            for f in files:
                os.lchown(os.path.join(root, f), uid, gid)


    #---
    def resubmit(self):
        next_run = self.counter + 1
        cmd = 'payu run -i {0} -n {1}'.format(next_run, self.n_runs)

        cmd = shlex.split(cmd)
        sp.Popen(cmd).wait()


    #---
    def sweep(self, hard_sweep=False):
        # TODO: Fix the IO race conditions!

        if hard_sweep:
            if os.path.isdir(self.archive_path):
                print 'Removing archive path %s' % self.archive_path
                #sh.rmtree(self.archive_path)
                cmd = 'rm -rf {0}'.format(self.archive_path).split()
                rc = sp.Popen(cmd).wait()
                assert rc == 0

            if os.path.islink(self.archive_sym_path):
                print 'Removing symlink %s' % self.archive_sym_path
                os.remove(self.archive_sym_path)

        if os.path.isdir(self.work_path):
            print 'Removing work path %s' % self.work_path
            #sh.rmtree(self.work_path)
            cmd = 'rm -rf {0}'.format(self.work_path).split()
            rc = sp.Popen(cmd).wait()
            assert rc == 0

        if os.path.islink(self.work_sym_path):
            print 'Removing symlink %s' % self.work_sym_path
            os.remove(self.work_sym_path)

        # TODO: model outstreams and pbs logs need to be handled separately
        logs = [f for f in os.listdir(os.curdir) if os.path.isfile(f) and
                (f == self.stdout_fname or
                 f == self.stderr_fname or
                 f.startswith(self.job_name + '.o') or
                 f.startswith(self.job_name + '.e') or
                 f.startswith(self.job_name + '_c.o') or
                 f.startswith(self.job_name + '_c.e')
                 )
                ]

        pbs_log_path = os.path.join(os.curdir, 'pbs_logs')
        mkdir_p(pbs_log_path)

        for f in logs:
            print 'Moving log {fname}'.format(fname=f)
            os.rename(f, os.path.join(pbs_log_path, f))
