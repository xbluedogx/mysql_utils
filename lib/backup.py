import boto
import os
import re
import resource
import subprocess
import time
import urllib
from boto.utils import get_instance_metadata

import mysql_lib
import host_utils
from lib import environment_specific


BACKUP_LOCK_FILE = '/tmp/backup_mysql.lock'
PV = '/usr/bin/pv -peafbt'
SSH_OPTIONS = '-q -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no'
SSH_AUTH = '-i /home/dbutil/.ssh/id_rsa dbutil'
S3_SCRIPT = '/usr/local/bin/gof3r'
TEMP_DIR = '/backup/tmp/xtrabackup'
TARGET_DIR = '/backup/mysql'
XB_RESTORE_STATUS = ("CREATE TABLE IF NOT EXISTS test.xb_restore_status ("
                     "id                INT UNSIGNED NOT NULL AUTO_INCREMENT, "
                     "restore_source    VARCHAR(64), "
                     "restore_type      ENUM('s3', 'remote_server', 'local_file') NOT NULL, "
                     "test_restore      ENUM('normal', 'test') NOT NULL, "
                     "restore_destination   VARCHAR(64), "
                     "restore_date      DATE, "
                     "restore_port      SMALLINT UNSIGNED NOT NULL DEFAULT 3306, "
                     "restore_file      VARCHAR(255), "
                     "replication       ENUM('SKIP', 'REQ', 'OK', 'FAIL'), "
                     "zookeeper         ENUM('SKIP', 'REQ', 'OK', 'FAIL'), "
                     "started_at        DATETIME NOT NULL, "
                     "finished_at       DATETIME, "
                     "restore_status    ENUM('OK', 'IPR', 'BAD') DEFAULT 'IPR', "
                     "status_message    TEXT, "
                     "PRIMARY KEY(id), "
                     "INDEX (restore_type, started_at), "
                     "INDEX (restore_type, restore_status, started_at) )")
XTRA_DEFAULTS = ' '.join(('--slave-info',
                          '--safe-slave-backup',
                          '--parallel=8',
                          '--stream=xbstream',
                          '--no-timestamp',
                          '--compress',
                          '--compress-threads=8',
                          '--kill-long-queries-timeout=10'))
XBSTREAM_SUFFIX = '.xbstream'
MINIMUM_VALID_BACKUP_SIZE_BYTES = 1024 * 1024

log = environment_specific.setup_logging_defaults(__name__)


def parse_xtrabackup_slave_info(datadir):
    """ Pull master_log and master_log_pos from a xtrabackup_slave_info file
    NOTE: This file has its data as a CHANGE MASTER command. Example:
    CHANGE MASTER TO MASTER_LOG_FILE='mysql-bin.006233', MASTER_LOG_POS=863

    Args:
    datadir - the path to the restored datadir

    Returns:
    binlog_file - Binlog file to start reading from
    binlog_pos - Position in binlog_file to start reading
    """
    file_path = os.path.join(datadir, 'xtrabackup_slave_info')
    with open(file_path) as f:
        data = f.read()

    file_pattern = ".*MASTER_LOG_FILE='([a-z0-9-.]+)'.*"
    pos_pattern = ".*MASTER_LOG_POS=([0-9]+).*"
    res = re.match(file_pattern, data)
    binlog_file = res.group(1)
    res = re.match(pos_pattern, data)
    binlog_pos = int(res.group(1))

    log.info('Master info: binlog_file: {binlog_file},'
             ' binlog_pos: {binlog_pos}'.format(binlog_file=binlog_file,
                                                binlog_pos=binlog_pos))
    return (binlog_file, binlog_pos)


def parse_xtrabackup_binlog_info(datadir):
    """ Pull master_log and master_log_pos from a xtrabackup_slave_info file
    Note: This file stores its data as two strings in a file
          deliminted by a tab. Example: "mysql-bin.006231\t1619"

    Args:
    datadir - the path to the restored datadir

    Returns:
    binlog_file - Binlog file to start reading from
    binlog_pos - Position in binlog_file to start reading
    """
    file_path = os.path.join(datadir, 'xtrabackup_binlog_info')
    with open(file_path) as f:
        data = f.read()

    fields = data.strip().split("\t")
    if len(fields) != 2:
        raise Exception(('Error: Invalid format in '
                         'file {file_path}').format(file_path=file_path))
    binlog_file = fields[0].strip()
    binlog_pos = int(fields[1].strip())

    log.info('Master info: binlog_file: {binlog_file},'
             ' binlog_pos: {binlog_pos}'.format(binlog_file=binlog_file,
                                                binlog_pos=binlog_pos))
    return (binlog_file, binlog_pos)


def get_host_from_backup(full_path):
    """ Parse the filename of a backup to determine the source of a backup

    Note: there is a strong assumption that the port number matches 330[0-9]

    Args:
    full_path - Path to a backup file.
                Example: /backup/tmp/mysql-legaldb001c-3306-2014-06-06.xbstream
                Example: /backup/tmp/mysql-legaldb-1-1-3306-2014-06-06.xbstream

    Returns:
    host - A hostaddr object
    """
    filename = os.path.basename(full_path)
    pattern = 'mysql-([a-z0-9-]+)-(330[0-9])-.+'
    res = re.match(pattern, filename)
    return host_utils.HostAddr(''.join((res.group(1),
                                        ':',
                                        res.group(2))))


def remove_backups(backup_path,
                   extension,
                   keep_newest=1):
    """ Remove old database backup files

    Args:
    backup_path - A string which is the path to the backup directory.
    extension - A tuple of extensions of files to be acted on. Note
                '.log' files of the same name of files being purged
                will also be removed.
    keep_newest - How many backups should be kept.
    """
    # Get list of backup files in the path specified
    files = list()
    for entry in os.listdir(backup_path):
        if entry.endswith(extension):
            fullpath = os.path.join(backup_path, entry)
            files.append((os.stat(fullpath).st_mtime, fullpath))
    files.sort()

    to_delete = max(0, len(files) - keep_newest)
    for entry in files[:to_delete]:
        log.info('Deleting backup file: {}'.format(entry[1]))
        os.remove(entry[1])

        log_file = ''.join((entry[1], '.log'))
        if os.path.isfile(log_file):
            log.info('Deleting log file: {log}'.format(log=log_file))
            os.remove(log_file)


def xtrabackup_instance(instance):
    """ Take a compressed mysql backup

    Args:
    instance - A hostaddr instance

    Returns:
    A string of the path to the finished backup
    """
    # Prevent issues with too many open files
    resource.setrlimit(resource.RLIMIT_NOFILE, (131072, 131072))
    (temp_path, target_path) = get_paths(port=str(instance.port))
    backup_file = ("mysql-{host}-{port}-{timestamp}.xbstream"
                   ).format(host=instance.hostname,
                            port=str(instance.port),
                            timestamp=time.strftime('%Y-%m-%d-%H:%M:%S'))
    tmp_xtra_path = os.path.join(temp_path, backup_file)
    target_xtra_path = os.path.join(target_path, backup_file)
    tmp_log = ''.join((tmp_xtra_path, '.log'))
    target_log = ''.join((tmp_xtra_path, '.log'))

    if host_utils.get_hiera_role() in host_utils.MASTERFUL_PUPPET_ROLES:
        cnf = host_utils.OLD_CONF_ROOT.format(port=instance.port)
        cnf_group = 'mysqld'
    else:
        cnf = host_utils.MYSQL_CNF_FILE
        cnf_group = 'mysqld{port}'.format(port=instance.port)
    datadir = host_utils.get_cnf_setting('datadir', instance.port)
    xtra_user, xtra_pass = mysql_lib.get_mysql_user_for_role('xtrabackup')

    cmd = ('/bin/bash -c "/usr/bin/innobackupex {datadir} {XTRA_DEFAULTS} '
           '--user={xtra_user} --password={xtra_pass} '
           '--defaults-file={cnf} --defaults-group={cnf_group} '
           '--port={port} 2>{tmp_log} '
           '>{dest}"').format(datadir=datadir,
                              XTRA_DEFAULTS=XTRA_DEFAULTS,
                              xtra_user=xtra_user,
                              xtra_pass=xtra_pass,
                              cnf=cnf,
                              cnf_group=cnf_group,
                              port=instance.port,
                              tmp_log=tmp_log,
                              dest=tmp_xtra_path)

    log.info(cmd)
    xtra = subprocess.Popen(cmd, shell=True)
    xtra.wait()
    with open(tmp_log, 'r') as log_file:
        xtra_log = log_file.readlines()
        if 'innobackupex: completed OK!' not in xtra_log[-1]:
            raise Exception('innobackupex failed. '
                            'log_file: {tmp_log}'.format(tmp_log=tmp_log))

    log.info('Moving backup and log to {target}'.format(target=target_path))
    os.rename(tmp_xtra_path, target_xtra_path)
    os.rename(tmp_log, target_log)
    log.info('Xtrabackup was successful')
    return target_xtra_path


def xbstream_unpack(xbstream, port, restore_source, restore_type, size=None):
    """ Decompress an xbstream filename into a directory.

    Args:
    xbstream - A string which is the path to the xbstream file
    port - The port on which to act on on localhost
    host - A string which is a hostname if the xbstream exists on a remote host
    size - An int for the size in bytes for remote unpacks for a progress bar
    """
    (temp_path, target_path) = get_paths(port)
    temp_backup = os.path.join(temp_path, os.path.basename(xbstream))
    datadir = host_utils.get_cnf_setting('datadir', port)

    if restore_type == 's3':
        cmd = ('{s3_script} get --no-md5 -b {bucket} -k {xbstream} '
               '2>/dev/null ').format(s3_script=S3_SCRIPT,
                                      bucket=environment_specific.S3_BUCKET,
                                      xbstream=urllib.quote_plus(xbstream),
                                      temp_backup=temp_backup)

    elif restore_type == 'local_file':
        cmd = '{pv} {xbstream}'.format(pv=PV,
                                       xbstream=xbstream)
    elif restore_type == 'remote_server':
        cmd = ("ssh {ops} {auth}@{host} '/bin/cat {xbstream}' "
               "").format(ops=SSH_OPTIONS,
                          auth=SSH_AUTH,
                          host=restore_source.hostname,
                          xbstream=xbstream,
                          temp_backup=temp_backup)
    else:
        raise Exception('Restore type {restore_type} is not supported'.format(restore_type=restore_type))

    if size and restore_type != 'localhost':
        cmd = ' | '.join((cmd, '{pv} -s {size}'.format(pv=PV,
                                                       size=str(size))))
    # And finally pipe everything into xbstream to unpack it
    cmd = ' | '.join((cmd, '/usr/bin/xbstream -x -C {datadir}'.format(datadir=datadir)))
    log.info(cmd)

    extract = subprocess.Popen(cmd, shell=True)
    if extract.wait() != 0:
        raise Exception("Error: Xbstream decompress did not succeed, aborting")


def innobackup_decompress(port, threads=8):
    """ Decompress an unpacked backup compressed with xbstream.

    Args:
    port - The port of the instance on which to act
    threads - A int which signifies how the amount of parallelism. Default is 8
    """
    datadir = host_utils.get_cnf_setting('datadir', port)

    cmd = ' '.join(('/usr/bin/innobackupex',
                    '--parallel={threads}',
                    '--decompress',
                    datadir)).format(threads=threads)

    err_log = os.path.join(datadir, 'xtrabackup-decompress.err')
    out_log = os.path.join(datadir, 'xtrabackup-decompress.log')

    with open(err_log, 'w+') as err_handle, open(out_log, 'w') as out_handle:
        verbose = '{cmd} 2>{err_log} >{out_log}'.format(cmd=cmd,
                                                        err_log=err_log,
                                                        out_log=out_log)
        log.info(verbose)
        decompress = subprocess.Popen(cmd,
                                      shell=True,
                                      stdout=out_handle,
                                      stderr=err_handle)
        if decompress.wait() != 0:
            raise Exception('Fatal error: innobackupex decompress '
                            'did not return 0')

        err_handle.seek(0)
        log_data = err_handle.readlines()
        if 'innobackupex: completed OK!' not in log_data[-1]:
            msg = ('Fatal error: innobackupex decompress did not end with ',
                   '"innobackupex: completed OK"')
            raise Exception(msg)


def apply_log(port, memory='10G'):
    """ Apply redo logs for an unpacked and uncompressed instance

    Args:
    path - The port of the instance on which to act
    memory - A string of how much memory can be used to apply logs. Default 10G
    """
    datadir = host_utils.get_cnf_setting('datadir', port)
    cmd = ' '.join(('/usr/bin/innobackupex',
                    '--apply-log',
                    '--use-memory={memory}',
                    datadir)).format(memory=memory)

    log_file = os.path.join(datadir, 'xtrabackup-apply-logs.log')
    with open(log_file, 'w+') as log_handle:
        verbose = '{cmd} >{log_file}'.format(cmd=cmd,
                                             log_file=log_file)
        log.info(verbose)
        apply_logs = subprocess.Popen(cmd,
                                      shell=True,
                                      stderr=log_handle)
        if apply_logs.wait() != 0:
            raise Exception('Fatal error: innobackupex apply-logs did not '
                            'return return 0')

        log_handle.seek(0)
        log_data = log_handle.readlines()
        if 'innobackupex: completed OK!' not in log_data[-1]:
            msg = ('Fatal error: innobackupex apply-log did not end with ',
                   '"innobackupex: completed OK"')
            raise Exception(msg)


def get_remote_backup(hostaddr, date=None):
    """ Find the most recent xbstream file on the desired instance

    Args:
    hostaddr - A hostaddr object for the desired instance
    date - desire date of restore file

    Returns:
    filename - The path to the most recent xbstream file
    size - The size of the backup file
    """
    path = os.path.join(TARGET_DIR, str(hostaddr.port))
    if date:
        date_param = '{date}*'.format(date=date)
    else:
        date_param = ''
    cmd = ("ssh {ops} {auth}@{host} 'ls -tl {path}/*{date_param}.xbstream |"
           " head -n1'").format(ops=SSH_OPTIONS,
                                auth=SSH_AUTH,
                                host=hostaddr.hostname,
                                path=path,
                                date_param=date_param)
    log.info(cmd)

    # We really only care here if there is output. Basically we will
    # ignore return code and stderr if we get stdout
    (out, err, _) = host_utils.shell_exec(cmd)

    if not out:
        msg = 'No backup found for {host} in {path}.\nError: {err}'
        raise Exception(msg.format(host=hostaddr.hostname,
                                   path=path,
                                   err=err))

    entries = out.split()
    size = entries[4]
    filename = entries[8]

    # Probably unlikely that we'd have a remote-server backup that's too small,
    # but we check anyway.
    if size < MINIMUM_VALID_BACKUP_SIZE_BYTES:
        msg = ('Found backup {backup_file} for host {host} '
               'but it is too small... '
               '({size} bytes < {min_size} bytes) '
               'Ignoring it.').format(backup_file=filename,
                                      host=hostaddr.hostname,
                                      size=size,
                                      min_size=MINIMUM_VALID_BACKUP_SIZE_BYTES)
        raise Exception(msg)

    msg = ('Found a backup {filename} with a '
           'size of {size}').format(size=size,
                                    filename=filename)
    log.debug(msg)
    return filename, size


def s3_upload(backup_file):
    """ Upload a backup file to s3.

    Args:
    backup_file - The file to be uploaded
    """
    cmd = ("{pv} {backup_file} | "
           "{S3_SCRIPT} put --key={s3_file} --bucket={S3_BUCKET} 2>/dev/null"
           "".format(pv=PV,
                     S3_BUCKET=environment_specific.S3_BUCKET,
                     S3_SCRIPT=S3_SCRIPT,
                     s3_file=urllib.quote_plus(os.path.basename(backup_file)),
                     backup_file=backup_file))
    log.info(cmd)
    upload = subprocess.Popen(cmd, shell=True)
    if upload.wait() != 0:
        raise Exception("Error: Upload to s3 failed.")


def get_s3_backup(hostaddr, date=None):
    """ Find the most recent xbstream file for an instance on s3

    Args:
    hostaddr - A hostaddr object for the desired instance
    date - Desired date of restore file

    Returns:
    filename - The path to the most recent xbstream file
    """
    prefix = 'mysql-{host}-{port}'.format(host=hostaddr.hostname,
                                          port=hostaddr.port)
    if date:
        prefix = ''.join((prefix, '-', date))
    log.debug('looking for backup with prefix {prefix}'.format(prefix=prefix))

    # by default, we try to do this the "old" way.  if we return 403,
    # we'll retry the "new" way.
    try:
        conn = boto.connect_s3()
        bucket = conn.get_bucket(environment_specific.S3_BUCKET, validate=False)
        bucket_items = bucket.get_all_keys(prefix=prefix)
    except:
        ROLE = 'base'
        md = get_instance_metadata()
        creds = md['iam']['security-credentials'][ROLE]
        conn = boto.connect_s3(aws_access_key_id=creds['AccessKeyId'],
                               aws_secret_access_key=creds['SecretAccessKey'],
                               security_token=creds['Token'])
        bucket = conn.get_bucket(environment_specific.S3_BUCKET, validate=False)
        bucket_items = bucket.get_all_keys(prefix=prefix)

    latest_backup = None
    for elem in bucket_items:
        # don't even consider files that aren't large enough.
        if XBSTREAM_SUFFIX in elem.name and elem.size > MINIMUM_VALID_BACKUP_SIZE_BYTES:
            if not latest_backup or elem.last_modified > latest_backup.last_modified:
                latest_backup = elem
    if not latest_backup:
        msg = ('Unable to find a valid backup for '
               '{instance}').format(instance=hostaddr)
        raise Exception(msg)
    log.debug('Found a s3 backup {s3_path} with a size of '
              '{size}'.format(s3_path=latest_backup.name,
                              size=latest_backup.size))
    return (latest_backup.name, latest_backup.size)


def start_restore_log(instance, params):
    """ Create a record in xb_restore_status at the start of a restore
    """
    try:
        conn = mysql_lib.connect_mysql(instance)
    except Exception as e:
        log.warning("Unable to connect to master to log "
                    "our progress: {e}.  Attempting to "
                    "continue with restore anyway.".format(e=e))
        return None

    if not mysql_lib.does_table_exist(conn, 'test', 'xb_restore_status'):
        create_status_table(conn)
    sql = ("REPLACE INTO test.xb_restore_status "
           "SET "
           "restore_source = %(restore_source)s, "
           "restore_type = %(restore_type)s, "
           "restore_file = %(restore_file)s, "
           "restore_destination = %(source_instance)s, "
           "restore_date = %(restore_date)s, "
           "restore_port = %(restore_port)s, "
           "replication = %(replication)s, "
           "zookeeper = %(zookeeper)s, "
           "started_at = NOW()")
    cursor = conn.cursor()
    try:
        cursor.execute(sql, params)
        log.info(cursor._executed)
        row_id = cursor.lastrowid
    except Exception as e:
        log.warning("Unable to log restore_status: {e}".format(e=e))
        row_id = None

    cursor.close()
    conn.commit()
    conn.close()
    return row_id


def update_restore_log(instance, row_id, params):
    try:
        conn = mysql_lib.connect_mysql(instance)
    except Exception as e:
        log.warning("Unable to connect to master to log "
                    "our progress: {e}.  Attempting to "
                    "continue with restore anyway.".format(e=e))
        return

    updates_fields = []

    if 'finished_at' in params:
        updates_fields.append('finished_at=NOW()')
    if 'restore_status' in params:
        updates_fields.append('restore_status=%(restore_status)s')
    if 'status_message' in params:
        updates_fields.append('status_message=%(status_message)s')
    if 'replication' in params:
        updates_fields.append('replication=%(replication)s')
    if 'zookeeper' in params:
        updates_fields.append('zookeeper=%(zookeeper)s')
    if 'finished_at' in params:
        updates_fields.append('finished_at=NOW()')

    sql = ("UPDATE test.xb_restore_status SET "
           + ', '.join(updates_fields) +
           " WHERE id = %(row_id)s")
    params['row_id'] = row_id
    cursor = conn.cursor()
    cursor.execute(sql, params)
    log.info(cursor._executed)
    cursor.close()
    conn.commit()
    conn.close()


def create_status_table(conn):
    """ Create the restoration status table if it isn't already there.

        Args:
            conn: A connection to the master server for this replica set.

        Returns:
            nothing
    """
    try:
        cursor = conn.cursor()
        cursor.execute(XB_RESTORE_STATUS)
        cursor.close()
    except Exception as e:
        log.error("Unable to create replication status table "
                  "on master: {e}".format(e=e))
        log.error("We will attempt to continue anyway.")


def quick_test_replication(instance):
    conn = mysql_lib.connect_mysql(instance)
    cursor = conn.cursor()
    cursor.execute('START SLAVE')
    time.sleep(2)
    ss = mysql_lib.get_slave_status(conn)
    if ss['Slave_IO_Running'] == 'No':
        raise Exception('Replication [IO THREAD] failed '
                        'to start: {e}'.format(e=ss['Last_IO_Error']))
    if ss['Slave_SQL_Running'] == 'No':
        raise Exception('Replication [SQL THREAD] failed '
                        'to start: {e}'.format(e=ss['Last_SQL_Error']))


def get_paths(port):
    """ Fetch the scratch and final directory for backups

    Args:
    port - An int which corresponds to the port on which the MySQL instance
           listens

    Returns:
    temp_path -  A string which is a path for scratch files. This path is
                 aggressively purged
    target_path - A string which is a path store sane backups. This path is
                  purged selectively to keep some number of backups.
    """
    temp_path = os.path.join(TEMP_DIR, str(port))
    target_path = os.path.join(TARGET_DIR, str(port))
    for path in [temp_path, target_path]:
        if not os.path.exists(path):
            log.info("{path} does not exist, creating".format(path=path))
            os.makedirs(path)

    return temp_path, target_path
