# Copyright (C) 2015 Sebastian Pipping <sebastian@pipping.org>
# Licensed under AGPL v3 or later

from __future__ import print_function

import errno
import os
import pwd
import stat
import subprocess
import tempfile
import time

from textwrap import dedent

from directory_bootstrap.shared.commands import \
        check_for_commands, find_command, check_call__keep_trying, \
        EXIT_COMMAND_NOT_FOUND, \
        COMMAND_BLKID, COMMAND_CHMOD, COMMAND_CHROOT, \
        COMMAND_CP, COMMAND_KPARTX, COMMAND_MKDIR, \
        COMMAND_MKFS_EXT4, COMMAND_MOUNT, COMMAND_PARTED, \
        COMMAND_PARTPROBE, COMMAND_RM, COMMAND_RMDIR, \
        COMMAND_SED, COMMAND_TUNE2FS
from directory_bootstrap.shared.mount import try_unmounting, COMMAND_UMOUNT
from directory_bootstrap.shared.namespace import \
        unshare_current_process, set_hostname
from directory_bootstrap.shared.resolv_conf import filter_copy_resolv_conf

from image_bootstrap.mount import MountFinder
from image_bootstrap.types.uuid import require_valid_uuid


BOOTLOADER__AUTO = 'auto'
BOOTLOADER__CHROOT_GRUB2__DEVICE = 'chroot-grub2-device'
BOOTLOADER__CHROOT_GRUB2__DRIVE = 'chroot-grub2-drive'
BOOTLOADER__HOST_GRUB2__DEVICE = 'host-grub2-device'
BOOTLOADER__HOST_GRUB2__DRIVE = 'host-grub2-drive'
BOOTLOADER__NONE = 'none'

_MOUNTPOINT_PARENT_DIR = '/mnt'
_CHROOT_SCRIPT_TARGET_DIR = 'root/chroot-scripts/'

_NON_DISK_MOUNT_TASKS = (
        ('/dev', ['-o', 'bind'], 'dev'),
        ('/dev/pts', ['-o', 'bind'], 'dev/pts'),
        ('PROC', ['-t', 'proc'], 'proc'),
        ('/sys', ['-o', 'bind'], 'sys'),
        )

_DISK_ID_OFFSET = 440
_DISK_ID_COUNT_BYTES = 4


class BootstrapEngine(object):
    def __init__(self,
            messenger,
            executor,
            hostname,
            architecture,
            root_password,
            abs_root_password_file,
            abs_etc_resolv_conf,
            disk_id,
            first_partition_uuid,
            abs_scripts_dir_pre,
            abs_scripts_dir_chroot,
            abs_scripts_dir_post,
            abs_target_path,
            command_grub2_install,
            bootloader_approach,
            bootloader_force,
            with_openstack,
            ):
        self._messenger = messenger
        self._executor = executor
        self._hostname = hostname
        self._architecture = architecture
        self._root_password = root_password
        self._abs_root_password_file = abs_root_password_file
        self._abs_etc_resolv_conf = abs_etc_resolv_conf
        self._disk_id = disk_id
        self._abs_scripts_dir_pre = abs_scripts_dir_pre
        self._abs_scripts_dir_chroot = abs_scripts_dir_chroot
        self._abs_scripts_dir_post = abs_scripts_dir_post
        self._abs_target_path = abs_target_path

        self._command_grub2_install = command_grub2_install
        self._bootloader_approach = bootloader_approach
        self._bootloader_force = bootloader_force
        self._with_openstack = with_openstack

        self._abs_mountpoint = None
        self._abs_first_partition_device = None
        self._first_partition_uuid = first_partition_uuid

        self._distro = None

    def set_distro(self, distro):
        self._distro = distro

    def check_release(self):
        return self._distro.check_release()

    def select_bootloader(self):
        if self._bootloader_approach == BOOTLOADER__AUTO:
            self._bootloader_approach = self._distro.select_bootloader()
            self._messenger.info('Selected approach "%s" for bootloader installation.'
                    % self._bootloader_approach)

    def get_commands_to_check_for(self):
        return list(self._distro.get_commands_to_check_for()) + [
                COMMAND_BLKID,
                COMMAND_CHMOD,
                COMMAND_CHROOT,
                COMMAND_CP,
                COMMAND_KPARTX,
                COMMAND_MKDIR,
                COMMAND_MKFS_EXT4,
                COMMAND_MOUNT,
                COMMAND_PARTED,
                COMMAND_PARTPROBE,
                COMMAND_RM,
                COMMAND_RMDIR,
                COMMAND_SED,
                COMMAND_TUNE2FS,
                COMMAND_UMOUNT,
                self._command_grub2_install,
                ]

    def _protect_against_grub_legacy(self, command):
        output = subprocess.check_output([command, '--version'])
        if 'GRUB GRUB 0.' in output:
            raise ValueError('Command "%s" is GRUB legacy while GRUB 2 is needed. '
                    'Please install GRUB 2 or pass --grub2-install .. on the command line.' \
                    % command)

    def detect_grub2_install(self):
        if self._command_grub2_install:
            return  # Explicit command given, no detection needed

        if self._bootloader_approach not in (
                BOOTLOADER__HOST_GRUB2__DEVICE,
                BOOTLOADER__HOST_GRUB2__DRIVE,
                ):
            return  # Host grub2-install not used, no detection needed

        COMMAND_GRUB_INSTALL = 'grub-install'
        COMMAND_GRUB2_INSTALL = 'grub2-install'

        self._command_grub2_install = COMMAND_GRUB2_INSTALL
        try:
            find_command(self._command_grub2_install)
        except OSError as e:
            if e.errno != EXIT_COMMAND_NOT_FOUND:
                raise

            self._command_grub2_install = COMMAND_GRUB_INSTALL
            try:
                find_command(self._command_grub2_install)
            except OSError as e:
                if e.errno != EXIT_COMMAND_NOT_FOUND:
                    raise

                # NOTE: consecutive search for "grub-install" will fail and
                #       be reported, so we don't need to raise here
                return

            self._protect_against_grub_legacy(self._command_grub2_install)

    def check_for_commands(self):
        check_for_commands(self._messenger, self.get_commands_to_check_for())

    def check_target_block_device(self):
        self._messenger.info('Checking if "%s" is a block device...' % self._abs_target_path)
        props = os.stat(self._abs_target_path)
        if not stat.S_ISBLK(props.st_mode):
            raise OSError(errno.ENOTBLK, 'Not a block device: "%s"' % self._abs_target_path)

    def check_architecture(self):
        self._messenger.info('Checking for known unsupported architecture/machine combination...')
        self._architecture = self._distro.check_architecture(self._architecture)
        assert self._architecture is not None

    def _script_should_be_run(self, basename):
        if basename.startswith('.'):
            return False
        elif basename.endswith('~'):
            return False
        return True

    def check_script_permissions(self):
        infos_produced = False

        good_uids = set()
        good_uids.add(os.geteuid())
        try:
            sudo_uid = int(os.environ['SUDO_UID'])
        except (KeyError, ValueError):
            pass
        else:
            good_uids.add(sudo_uid)

        for category, abs_scripts_dir in (
                ('pre-chroot', self._abs_scripts_dir_pre),
                ('chroot', self._abs_scripts_dir_chroot),
                ('post-chroot', self._abs_scripts_dir_post),
                ):
            if abs_scripts_dir is None:
                continue

            self._messenger.info('Checking %s scripts directory permissions...' % category)
            infos_produced = True

            props = os.lstat(abs_scripts_dir)
            if stat.S_ISLNK(props.st_mode):
                raise OSError(errno.ENOTDIR, 'Directory "%s" is a symlink. Only true directories are supported.' % abs_scripts_dir)

            if not stat.S_ISDIR(props.st_mode):
                raise OSError(errno.ENOTDIR, 'Directory "%s" is not a directory' % abs_scripts_dir)

            if props.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise OSError(errno.EPERM, 'Directory "%s" is writable to users other than its owner' % abs_scripts_dir)

            if props.st_uid not in good_uids:
                user_info = ' or '.join(('user %s/%d' % (pwd.getpwuid(uid).pw_name, uid) for uid in sorted(good_uids)))
                raise OSError(errno.EPERM, 'Directory "%s" is not owned by %s' % (abs_scripts_dir, user_info))

            self._messenger.info('Checking %s scripts for executability...' % category)
            infos_produced = True

            for basename in os.listdir(abs_scripts_dir):
                if not self._script_should_be_run(basename):
                    continue

                abs_filename = os.path.join(abs_scripts_dir, basename)
                if not os.access(abs_filename, os.X_OK):
                    raise OSError(errno.EACCES, 'Permission denied, file "%s" not executable' % abs_filename)

        if infos_produced:
            self._messenger.info_gap()

    def _unshare(self):
        unshare_current_process(self._messenger)
        set_hostname(self._hostname)

    def _partition_device(self):
        self._messenger.info('Partitioning "%s"...' % self._abs_target_path)
        cmd_mklabel = [
                COMMAND_PARTED,
                '--script',
                self._abs_target_path,
                'mklabel', 'msdos',
                ]
        self._executor.check_call(cmd_mklabel)

        # Make existing partition devices leave
        check_call__keep_trying(self._executor, [
                COMMAND_PARTPROBE,
                self._abs_target_path,
                ])

        cmd_mkpart = [
                COMMAND_PARTED,
                '--script',
                '--align', 'optimal',
                self._abs_target_path,
                'mkpart',
                'primary', 'ext4', '1', '100%',
                ]
        self._executor.check_call(cmd_mkpart)

        cmd_boot_flag = [
                COMMAND_PARTED,
                '--script',
                self._abs_target_path,
                'set', '1', 'boot', 'on',
                ]
        time.sleep(1)  # increase chances of first call working, e.g. with LVM volumes
        check_call__keep_trying(self._executor, cmd_boot_flag)

    def _create_partition_devices(self):
        self._messenger.info('Activating partition devices...')
        cmd_list = [
                COMMAND_KPARTX,
                '-l',
                self._abs_target_path,
                ]
        output = self._executor.check_output(cmd_list)
        device_name = output.split('\n')[0].split(' : ')[0]
        self._abs_first_partition_device = '/dev/mapper/%s' % device_name

        if True:
            cmd_add = [
                    COMMAND_KPARTX,
                    '-u',
                    '-s',
                    self._abs_target_path,
                    ]
            self._executor.check_call(cmd_add)

        for i in range(3):
            if os.path.exists(self._abs_first_partition_device):
                break
            time.sleep(1)
        else:
            raise OSError(errno.ENOENT, "No such block device file: '%s'" \
                    % self._abs_first_partition_device)

    def _format_partitions(self):
        self._messenger.info('Creating file system on "%s"...' % self._abs_first_partition_device)
        cmd = [
                COMMAND_MKFS_EXT4,
                '-F',
                self._abs_first_partition_device,
                ]
        self._executor.check_call(cmd)

    def _mkdir_mountpount(self):
        self._abs_mountpoint = tempfile.mkdtemp(dir=_MOUNTPOINT_PARENT_DIR)
        self._messenger.info('Creating directory "%s"...' % self._abs_mountpoint)

    def _mkdir_mountpount_etc(self):
        abs_dir = os.path.join(self._abs_mountpoint, 'etc')
        self._messenger.info('Creating directory "%s"...' % abs_dir)
        os.mkdir(abs_dir, 0755)

    def _mount_disk_chroot_mounts(self):
        self._messenger.info('Mounting partitions...')
        cmd = [
                COMMAND_MOUNT,
                self._abs_first_partition_device,
                self._abs_mountpoint,
                ]
        self._executor.check_call(cmd)

    def run_directory_bootstrap(self):
        return self._distro.run_directory_bootstrap(
                self._abs_mountpoint,
                self._architecture,
                self._bootloader_approach,
                )

    def _unmount_directory_bootstrap_leftovers(self):
        mounts = MountFinder()
        mounts.load()
        for abs_mount_point in reversed(list(mounts.below(self._abs_mountpoint))):
            self._try_unmounting(abs_mount_point)

    def _set_root_password_inside_chroot(self):
        self._messenger.info('Setting root password...')
        if self._root_password is None:
            return

        cmd = [
                COMMAND_CHROOT,
                self._abs_mountpoint,
                'chpasswd',
                ]
        env = self.make_environment(tell_mountpoint=False)
        self._messenger.announce_command(cmd)
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, env=env)
        p.stdin.write('root:%s' % self._root_password)
        p.stdin.close()
        p.wait()
        if p.returncode:
            raise subprocess.CalledProcessError(p.returncode, cmd)

    def _set_first_partition_uuid(self):
        if not self._first_partition_uuid:
            return

        self._messenger.info('Setting first partition UUID to %s...' % self._first_partition_uuid)
        cmd = [COMMAND_TUNE2FS,
                '-U', self._first_partition_uuid,
                self._abs_first_partition_device,
                ]
        self._executor.check_call(cmd)

    def _gather_first_partition_uuid(self):
        cmd_blkid = [
                COMMAND_BLKID,
                '-o', 'value',
                '-s', 'UUID',
                self._abs_first_partition_device,
                ]
        output = self._executor.check_output(cmd_blkid)
        first_partition_uuid = output.rstrip()
        require_valid_uuid(first_partition_uuid)
        self._first_partition_uuid = first_partition_uuid

    def _create_etc_fstab(self):
        filename = os.path.join(self._abs_mountpoint, 'etc', 'fstab')
        self._messenger.info('Writing file "%s"...' % filename)
        f = open(filename, 'w')
        print('/dev/disk/by-uuid/%s / auto defaults 0 1' % self._first_partition_uuid, file=f)
        f.close()

    def _create_etc_hostname(self):
        filename = os.path.join(self._abs_mountpoint, 'etc', 'hostname')
        self._messenger.info('Writing file "%s"...' % filename)
        f = open(filename, 'w')
        print(self._hostname, file=f)
        f.close()

    def create_network_configuration(self):
        return self._distro.create_network_configuration(self._abs_mountpoint)

    def _fix_grub_cfg_root_device(self):
        self._messenger.info('Post-processing GRUB config...')
        cmd_sed = [
                COMMAND_SED,
                's,root=[^ ]\+,root=UUID=%s,g' % self._first_partition_uuid,
                '-i', os.path.join(self._abs_mountpoint, 'boot', 'grub', 'grub.cfg'),
                ]
        self._executor.check_call(cmd_sed)

    def _run_scripts_from(self, abs_scripts_dir, env):
        for basename in sorted(os.listdir(abs_scripts_dir)):
            if not self._script_should_be_run(basename):
                continue

            cmd = [os.path.join(abs_scripts_dir, basename)]
            self._executor.check_call(cmd, env=env.copy())

    def make_environment(self, tell_mountpoint):
        env = os.environ.copy()
        for key in ('LANG', 'LANGUAGE'):
            env.pop(key, None)
        env.update({
                'HOSTNAME': self._hostname,  # for compatibility to grml-debootstrap
                'IB_HOSTNAME': self._hostname,
                'LC_ALL': 'C',
                })
        if tell_mountpoint:
            env.update({
                    'IB_ROOT': self._abs_mountpoint,
                    'MNTPOINT': self._abs_mountpoint,  # for compatibility to grml-debootstrap
                    })
        return env

    def _run_pre_scripts(self):
        self._messenger.info('Running pre-chroot scripts...')
        env = self.make_environment(tell_mountpoint=True)
        if self._abs_scripts_dir_pre:
            self._run_scripts_from(self._abs_scripts_dir_pre, env)

    def _mount_nondisk_chroot_mounts(self):
        self._messenger.info('Mounting non-disk file systems...')
        for source, options, target in _NON_DISK_MOUNT_TASKS:
            cmd = [
                    COMMAND_MOUNT,
                    source,
                    ] \
                    + options \
                    + [
                        os.path.join(self._abs_mountpoint, target),
                    ]
            self._executor.check_call(cmd)

    def _create_bootloader_install_message(self, real_abs_target):
        hints = []
        if real_abs_target != os.path.normpath(self._abs_target_path):
            hints.append('actually "%s"' % real_abs_target)
        hints.append('approach "%s"' % self._bootloader_approach)

        return 'Installing bootloader to device "%s" (%s)...' % (
                self._abs_target_path, ', '.join(hints))

    def get_chroot_command_grub2_install(self):
        return self._distro.get_chroot_command_grub2_install()

    def _ensure_chroot_has_grub2_installed(self):
        env = self.make_environment(tell_mountpoint=False)
        self._distro.ensure_chroot_has_grub2_installed(self._abs_mountpoint, env)

    def _install_bootloader__grub2(self):
        real_abs_target = os.path.realpath(self._abs_target_path)
        message = self._create_bootloader_install_message(real_abs_target)

        use_chroot = self._bootloader_approach in (BOOTLOADER__CHROOT_GRUB2__DEVICE, BOOTLOADER__CHROOT_GRUB2__DRIVE)
        use_device_map = self._bootloader_approach in (BOOTLOADER__CHROOT_GRUB2__DRIVE, BOOTLOADER__HOST_GRUB2__DRIVE)

        if use_device_map:
            # Write device map just for being able to call grub-install
            abs_chroot_device_map = os.path.join(self._abs_mountpoint, 'boot', 'grub', 'device.map')
            grub_drive = '(hd0)'
            self._messenger.info('Writing device map to "%s" (mapping "%s" to "%s")...' \
                    % (abs_chroot_device_map, grub_drive, real_abs_target))
            f = open(abs_chroot_device_map, 'w')
            print('%s\t%s' % (grub_drive, real_abs_target), file=f)
            f.close()

        self._messenger.info(message)

        cmd = []

        if use_chroot:
            cmd += [
                COMMAND_CHROOT,
                self._abs_mountpoint,
                self.get_chroot_command_grub2_install(),
                ]
            env = self.make_environment(tell_mountpoint=False)
        else:
            cmd += [
                self._command_grub2_install,
                '--boot-directory',
                os.path.join(self._abs_mountpoint, 'boot'),
                ]
            env = None

        if self._bootloader_force:
            cmd.append('--force')

        if use_device_map:
            cmd.append(grub_drive)
        else:
            cmd.append(self._abs_target_path)

        self._executor.check_call(cmd, env=env)

        if use_device_map:
            os.remove(abs_chroot_device_map)

    def generate_grub_cfg_from_inside_chroot(self):
        env = self.make_environment(tell_mountpoint=False)
        return self._distro.generate_grub_cfg_from_inside_chroot(self._abs_mountpoint, env)

    def _adjust_initramfs_generator_config(self):
        self._distro.adjust_initramfs_generator_config(self._abs_mountpoint)

    def generate_initramfs_from_inside_chroot(self):
        self._messenger.info('Generating initramfs...')
        env = self.make_environment(tell_mountpoint=False)
        return self._distro.generate_initramfs_from_inside_chroot(self._abs_mountpoint, env)

    def _create_etc_resolv_conf(self):
        output_filename = os.path.join(self._abs_mountpoint, 'etc', 'resolv.conf')

        filter_copy_resolv_conf(self._messenger, self._abs_etc_resolv_conf, output_filename)

    def _copy_chroot_scripts(self):
        self._messenger.info('Copying chroot scripts into chroot...')
        abs_path_parent = os.path.join(self._abs_mountpoint, _CHROOT_SCRIPT_TARGET_DIR)
        cmd_mkdir = [
                COMMAND_MKDIR,
                abs_path_parent,
                ]
        self._executor.check_call(cmd_mkdir)
        for basename in os.listdir(self._abs_scripts_dir_chroot):
            if not self._script_should_be_run(basename):
                continue

            abs_path_source = os.path.join(self._abs_scripts_dir_chroot, basename)
            abs_path_target = os.path.join(self._abs_mountpoint, _CHROOT_SCRIPT_TARGET_DIR, basename)
            cmd_copy = [
                    COMMAND_CP,
                    abs_path_source,
                    abs_path_target,
                    ]
            self._executor.check_call(cmd_copy)
            cmd_chmod = [
                    COMMAND_CHMOD,
                    'a+x',
                    abs_path_target,
                    ]
            self._executor.check_call(cmd_chmod)

    def _run_chroot_scripts(self):
        self._messenger.info('Running chroot scripts...')
        env = self.make_environment(tell_mountpoint=False)
        for basename in os.listdir(self._abs_scripts_dir_chroot):
            if not self._script_should_be_run(basename):
                continue

            cmd_run = [
                    COMMAND_CHROOT,
                    self._abs_mountpoint,
                    os.path.join('/', _CHROOT_SCRIPT_TARGET_DIR, basename),
                    ]
            self._executor.check_call(cmd_run, env=env.copy())

    def _remove_chroot_scripts(self):
        self._messenger.info('Removing chroot scripts...')
        for basename in os.listdir(self._abs_scripts_dir_chroot):
            if not self._script_should_be_run(basename):
                continue

            abs_path_target = os.path.join(self._abs_mountpoint, _CHROOT_SCRIPT_TARGET_DIR, basename)
            cmd_rm = [
                    COMMAND_RM,
                    abs_path_target,
                    ]
            self._executor.check_call(cmd_rm)

        abs_path_parent = os.path.join(self._abs_mountpoint, _CHROOT_SCRIPT_TARGET_DIR)
        cmd_rmdir = [
                COMMAND_RMDIR,
                abs_path_parent,
                ]
        self._executor.check_call(cmd_rmdir)

    def _try_unmounting(self, abs_path):
        return try_unmounting(self._executor, abs_path)

    def _unmount_nondisk_chroot_mounts(self):
        self._messenger.info('Unmounting non-disk file systems...')
        for source, options, target in reversed(_NON_DISK_MOUNT_TASKS):
            abs_path = os.path.join(self._abs_mountpoint, target)
            self._try_unmounting(abs_path)

    def perform_post_chroot_clean_up(self):
        return self._distro.perform_post_chroot_clean_up(self._abs_mountpoint)

    def _run_post_scripts(self):
        self._messenger.info('Running post-chroot scripts...')
        env = self.make_environment(tell_mountpoint=True)
        if self._abs_scripts_dir_post:
            self._run_scripts_from(self._abs_scripts_dir_post, env)

    def _unmount_disk_chroot_mounts(self):
        self._messenger.info('Unmounting partitions...')
        self._try_unmounting(self._abs_mountpoint)

    def _remove_partition_devices(self):
        self._messenger.info('Deactivating partition devices...')
        cmd = [
                COMMAND_KPARTX,
                '-d',
                self._abs_target_path,
                ]
        check_call__keep_trying(self._executor, cmd)

    def _rmdir_mountpount(self):
        self._messenger.info('Removing directory "%s"...' % self._abs_mountpoint)
        for i in range(3):
            try:
                os.rmdir(self._abs_mountpoint)
            except OSError as e:
                if e.errno != errno.EBUSY:
                    raise
                time.sleep(1)
            else:
                break

    def _set_disk_id_in_mbr(self):
        if not self._disk_id:
            return

        content = self._disk_id.byte_sequence()
        assert len(content) == _DISK_ID_COUNT_BYTES

        self._messenger.info('Setting MBR disk identifier to %s (4 bytes)...' % str(self._disk_id))
        f = open(self._abs_target_path, 'w')
        f.seek(_DISK_ID_OFFSET)
        f.write(content)
        f.close()

    def process_root_password(self):
        if self._abs_root_password_file:
            self._messenger.info('Reading root password from file "%s"...' % self._abs_root_password_file)
            f = open(self._abs_root_password_file)
            self._root_password = f.read().split('\n')[0]
            f.close()
        elif self._root_password is not None:
            self._messenger.warn('Using --password PASSWORD is a security risk more often than not; '
                    'please consider using --password-file FILE, instead.')

    def _install_sudo(self):
        env = self.make_environment(tell_mountpoint=False)
        return self._distro.install_sudo(self._abs_mountpoint, env)

    def _create_sudo_nopasswd_user(self):
        user_name = self._distro.get_cloud_username()
        self._messenger.info('Creating user "%s"...' % user_name)
        cmd = [
                COMMAND_CHROOT,
                self._abs_mountpoint,
                'useradd',
                '--comment', 'Cloud-init-user',
                '--base-dir', '/home',
                '--create-home',
                '--shell', '/bin/bash',
                '--user-group', user_name,
                ]
        env = self.make_environment(tell_mountpoint=False)
        self._executor.check_call(cmd, env)

        self._messenger.info('Allowing user "%s" to call sudo with no password...' % user_name)
        sudoers_path = os.path.join(self._abs_mountpoint, 'etc/sudoers.d/%s-nopasswd' % user_name)
        with open(sudoers_path, 'w') as f:
            print('%s ALL = NOPASSWD: ALL' % user_name, file=f)
            os.fchmod(f.fileno(), 0440)

    def _install_cloud_init_and_friends(self):
        env = self.make_environment(tell_mountpoint=False)
        return self._distro.install_cloud_init_and_friends(self._abs_mountpoint, env)

    def _configure_cloud_init_and_friends(self):
        cloud_cfg_path = os.path.join(self._abs_mountpoint, 'etc/cloud/cloud.cfg')
        self._messenger.info('Adjusting file "%s"...' % cloud_cfg_path)
        with open(cloud_cfg_path) as f:
            content = f.read()

        lines_to_write = []
        for line in content.split('\n'):
            if line.startswith('     name:'):
                line = '     name: %s' % self._distro.get_cloud_username()

            lines_to_write.append(line)

        with open(cloud_cfg_path, 'w') as f:
            print('\n'.join(lines_to_write), file=f)

        cloud_cfg_d_file_path = os.path.join(self._abs_mountpoint,
                self._distro.get_cloud_init_datasource_cfg_path().lstrip('/'))
        self._messenger.info('Writing file "%s"...' % cloud_cfg_d_file_path)
        with open(cloud_cfg_d_file_path, 'w') as f:
            print(dedent("""\
                    # generated by image-bootstrap
                    datasource_list: [ConfigDrive, Openstack, Ec2]
                    """), file=f)

    def _install_sshd(self):
        env = self.make_environment(tell_mountpoint=False)
        return self._distro.install_sshd(self._abs_mountpoint, env)

    def _make_openstack_services_autostart(self):
        env = self.make_environment(tell_mountpoint=False)
        return self._distro.make_openstack_services_autostart(self._abs_mountpoint, env)

    def run(self):
        self._unshare()
        self._partition_device()
        self._set_disk_id_in_mbr()
        self._create_partition_devices()
        try:
            self._format_partitions()

            if self._first_partition_uuid:
                self._set_first_partition_uuid()
            else:
                self._gather_first_partition_uuid()
            assert self._first_partition_uuid

            self._mkdir_mountpount()
            try:
                self._mount_disk_chroot_mounts()
                try:
                    self._mkdir_mountpount_etc()
                    self._create_etc_hostname()  # first time
                    self._create_etc_resolv_conf()  # first time
                    try:
                        self.run_directory_bootstrap()
                    finally:
                        self._unmount_directory_bootstrap_leftovers()
                    self._create_etc_hostname()  # re-write
                    self._create_etc_resolv_conf()  # re-write
                    self._create_etc_fstab()
                    self.create_network_configuration()
                    self._run_pre_scripts()
                    if self._bootloader_approach in (BOOTLOADER__HOST_GRUB2__DEVICE, BOOTLOADER__HOST_GRUB2__DRIVE):
                        self._install_bootloader__grub2()
                    self._mount_nondisk_chroot_mounts()
                    try:
                        self._set_root_password_inside_chroot()

                        if self._bootloader_approach in (BOOTLOADER__CHROOT_GRUB2__DEVICE, BOOTLOADER__CHROOT_GRUB2__DRIVE):
                            self._ensure_chroot_has_grub2_installed()
                            self._install_bootloader__grub2()

                        if self._bootloader_approach != BOOTLOADER__NONE:
                            self._messenger.info('Generating GRUB configuration...')
                            self.generate_grub_cfg_from_inside_chroot()

                            self._fix_grub_cfg_root_device()

                        self._adjust_initramfs_generator_config()
                        self.generate_initramfs_from_inside_chroot()

                        if self._with_openstack:
                            self._install_sudo()
                            self._create_sudo_nopasswd_user()
                            self._install_cloud_init_and_friends()
                            self._configure_cloud_init_and_friends()
                            self._install_sshd()
                            self._make_openstack_services_autostart()

                        if self._abs_scripts_dir_chroot:
                            self._copy_chroot_scripts()
                            try:
                                self._run_chroot_scripts()
                            finally:
                                self._remove_chroot_scripts()
                    finally:
                        self._unmount_nondisk_chroot_mounts()
                    self.perform_post_chroot_clean_up()
                    self._run_post_scripts()
                finally:
                    self._unmount_disk_chroot_mounts()
            finally:
                self._rmdir_mountpount()
        finally:
            self._remove_partition_devices()