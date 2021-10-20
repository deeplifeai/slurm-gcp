#!/usr/bin/env python3

# Copyright 2017 SchedMD LLC.
# Modified for use with the Slurm Resource Manager.
#
# Copyright 2015 Google Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import logging
import os
import sys
import tempfile
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from itertools import groupby, islice

from addict import Dict as NSDict

import util
from util import run, chunked
from util import cfg, lkp, compute
from setup import resolve_network_storage


PREFIX = Path('/usr/local/bin')
SCONTROL = PREFIX/'scontrol'

cfg.log_dir = '/var/log/slurm'
LOGFILE = (Path(cfg.log_dir or '')/Path(__file__).name).with_suffix('.log')
SCRIPTS_DIR = Path(__file__).parent.resolve()

logger_name = Path(__file__).name
log = logging.getLogger(logger_name)


def instance_properties(partition_name):
    partition = cfg.partitions[partition_name]
    region = partition.region
    subnet = partition.subnet_name or f'{cfg.cluster_name}-subnet'

    props = NSDict()
    props.networkInterfaces = [{
        'subnetwork': f'projects/{cfg.project}/regions/{region}/subnetworks/{subnet}'
    }]

    compute_config = NSDict()
    compute_config.cluster_name = cfg.cluster_name
    compute_config.munge_key = cfg.munge_key
    compute_config.network_storage = resolve_network_storage(partition_name)

    metadata = {
        'cluster_name': cfg.cluster_name,
        'config': json.dumps(compute_config.to_dict()),
        'startup-script': Path('/slurm/scripts/startup.sh').read_text(),
        'instance_type': 'compute',
        'enable-oslogin': 'TRUE',
        'VmDnsSetting': 'GlobalOnly',
    }

    props.metadata['items'] = [
        NSDict({'key': k, 'value': v}) for k, v in metadata.items()
    ]
    return props


def create_instances(nodes):
    """ Call regionInstances.bulkInsert to create instances """
    if len(nodes) == 0:
        return
    # model here indicates any node that can be used to describe the rest
    model = next(iter(nodes))
    template = lkp.node_template_props(model).url
    partition_name = lkp.node_partition(model)
    partition = cfg.partitions[partition_name]

    body = NSDict()
    body.count = len(nodes)
    body.sourceInstanceTemplate = template
    body.perInstanceProperties = {k: {} for k in nodes}
    body.instanceProperties = instance_properties(partition_name)

    result = util.ensure_execute(compute.regionInstances().bulkInsert(
        project=cfg.project, region=partition.region, body=body
    ))
    return result


def expand_nodelist(nodelist):
    """ expand nodes in hostlist to hostnames """
    nodes = run(f"{SCONTROL} show hostnames {nodelist}").stdout.splitlines()
    return nodes


def resume_nodes(nodelist):
    """ resume nodes in nodelist """
    log.info(f"resume {nodelist}")
    nodes = expand_nodelist(nodelist)

    def ident_key(n):
        # ident here will refer to the combination of template and partition
        return lkp.node_template(n), lkp.node_partition(n)
    nodes.sort(key=ident_key)
    grouped_nodes = [
        (ident, chunk)
        for ident, nodes in groupby(nodes, ident_key)
        for chunk in chunked(nodes)
    ]
    log.debug(f"grouped_nodes: {grouped_nodes}")

    with ThreadPoolExecutor() as exe:
        futures = []
        for _, nodes in grouped_nodes:
            futures.append(exe.submit(create_instances, nodes))
        for f in futures:
            result = f.exception(timeout=60)
            if result:
                raise result


def prolog_resume_nodes(nodelist, job_id):
    pass


def main(nodelist, job_id):
    """ main called when run as script """
    log.debug(f"main nodelist={nodelist} job_id={job_id}")
    if job_id is not None:
        prolog_resume_nodes(nodelist, job_id)
    else:
        resume_nodes(nodelist)


parser = argparse.ArgumentParser(
    description=__doc__,
    formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument(
    'nodelist', help="list of nodes to resume"
)
parser.add_argument(
    'job_id', nargs='?', default=None,
    help="Optional job id for node list. Implies that PrologSlurmctld called program"
)
parser.add_argument('--debug', '-d', dest='debug', action='store_true',
                    help='Enable debugging output')


if __name__ == '__main__':
    util.config_root_logger(logger_name, level='DEBUG', util_level='DEBUG',
                            logfile=LOGFILE)
    log = logging.getLogger(Path(__file__).name)

    if "SLURM_JOB_NODELIST" in os.environ:
        argv = [
            *sys.argv[1:],
            os.environ['SLURM_JOB_NODELIST'],
            os.environ['SLURM_JOB_ID'],
        ]
        args = parser.parse_args(argv)
    else:
        args = parser.parse_args()

    if args.debug:
        util.config_root_logger(logger_name, level='DEBUG', util_level='DEBUG',
                                logfile=LOGFILE)
    else:
        util.config_root_logger(logger_name, level='INFO', util_level='ERROR',
                                logfile=LOGFILE)
    log = logging.getLogger(Path(__file__).name)
    sys.excepthook = util.handle_exception

    main(args.nodelist, args.job_id)
