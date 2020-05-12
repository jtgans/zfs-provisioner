import asyncio
import dataclasses
import itertools
import json
import logging
import os
import sys

from functools import wraps
from typing import Optional, Dict, List

import yaml

import click
import bitmath
import kopf

from kopf.clients import patching
from kopf.structs import patches
from kopf.structs import resources

import kubernetes
import kubernetes_asyncio


log = logging.getLogger('zfs-provisioner')


@dataclasses.dataclass
class Config:
    provisioner_name: str = 'asteven/zfs-provisioner'
    namespace: str = 'kube-system'
    default_parent_dataset: str = 'chaos/data/local-zfs-provisioner'
    dataset_mount_dir: str = '/var/lib/local-zfs-provisioner'
    container_image: str = 'asteven/zfs-provisioner'
    node_name: Optional[str] = None
    config: Optional[str] = None
    dataset_phase_annotations: Dict[str, str] = dataclasses.field(default_factory=dict)
    storage_classes: Dict[str, Dict] = dataclasses.field(default_factory=dict)


CONFIG = Config(
    dataset_phase_annotations={
        action:f'zfs-provisioner/dataset-phase-{action}'
        for action in ('create', 'delete', 'resize')
    }
)


def configure(**kwargs):
    for k,v in kwargs.items():
        if v is not None:
            setattr(CONFIG, k, v)


@dataclasses.dataclass
class StorageClass:
    """https://kubernetes.io/docs/reference/generated/kubernetes-api/v1.17/#storageclass-v1-storage-k8s-io"""
    name: str
    provisioner: str
    allowVolumeExpansion: bool = False
    mountOptions: List[str] = None
    parameters: Dict[str, str] = dataclasses.field(default_factory=dict)
    reclaimPolicy: str = 'Delete'
    volumeBindingMode: str = 'VolumeBindingImmediate'

    # Constants
    MODE_LOCAL: str = 'local'
    MODE_NFS: str = 'nfs'
    RECLAIM_POLICY_DELETE: str = 'Delete'
    RECLAIM_POLICY_RETAIN: str = 'Retain'

    @classmethod
    def from_dicts(cls, *dicts: List[Dict]) -> 'StorageClass':
        class_fields = {f.name for f in dataclasses.fields(cls)}
        items = itertools.chain(*[d.items() for d in dicts])
        return cls(**{k:v for k,v in items if k in class_fields})


def filter_provisioner(body, **_):
    return body.get('provisioner', None) == CONFIG.provisioner_name


@kopf.on.resume('storage.k8s.io', 'v1', 'storageclasses',
    when=filter_provisioner)
@kopf.on.create('storage.k8s.io', 'v1', 'storageclasses',
    when=filter_provisioner)
def cache_storage_class(name, body, meta, logger, **kwargs):
    """Load storage class properties and parameters from API server.
    """
    log.info('Watching for PVCs with storage class: %s', name)
    storage_class = StorageClass.from_dicts(meta, body)
    CONFIG.storage_classes[name] = storage_class
    log.debug('Caching storage class %s as: %s', name, storage_class)


def annotate_results(fn):
    """A decorator that persists handler results as annotations on the
    resource.

    Before calling a handler, load any existing results from
    annotations and pass them as the keyword argument 'results'
    to the handler.

    Store any outcome returned by the handler in the annotation.
    """
    key = 'zfs-provisioner/results'

    @wraps(fn)
    def wrapper(*args, **kwargs):
        meta = kwargs['meta']
        results_json = meta['annotations'].get(key, None)
        if results_json:
            results = json.loads(results_json)
        else:
            results = {}
        kwargs['results'] = results
        result = fn(*args, **kwargs)
        if result:
            results[fn.__name__] = result
            patch = kwargs['patch']
            patch.metadata.annotations[key] = json.dumps(results)

        # We don't return the result as we have handled it ourself.
        # Otherwise kopf tries to store it again in the objects status
        # which doesn't work anyway on k8s >= 1.16.
        #return result

    return wrapper


def get_template(template_file):
    template_path = os.path.join(os.path.dirname(__file__), 'templates', template_file)
    template = open(template_path, 'rt').read()
    return template


def get_example_pod(pod_name, node_name):
    template = get_template('example-pod.yaml')
    text = template.format(
        pod_name=pod_name,
        node_name=node_name,
    )
    data = yaml.safe_load(text)
    return data


def get_dataset_pod(pod_name, node_name, pod_args):
    template = get_template('dataset-pod.yaml')
    text = template.format(
        pod_name=pod_name,
        node_name=node_name,
        image=CONFIG.container_image,
        dataset_mount_dir=CONFIG.dataset_mount_dir,
    )
    data = yaml.safe_load(text)
    data['spec']['containers'][0]['args'] = pod_args
    return data


def filter_create_dataset(body, meta, spec, status, **_):
    """Filter function for resume, create and update handlers
    that filters out the PVCs for which the dataset creation
    process can be started.
    """
    # Only care about PVCs that are in state Pending.
    if status.get('phase', None) != 'Pending':
        return False

    # Only care about PVCs that we are not already working on.
    if CONFIG.dataset_phase_annotations['create'] in meta.annotations:
        return False

    handle_it = False
    try:
        # Only care about PVCs that have a storage class that we are responsible for.
        storage_class_name = spec['storageClassName']
        storage_class = CONFIG.storage_classes[storage_class_name]
        handle_it = True

        # Check storage class specific settings.
        if storage_class.volumeBindingMode == 'WaitForFirstConsumer':
            handle_it = 'volume.kubernetes.io/selected-node' in meta.annotations
    except KeyError:
        handle_it = False
    return handle_it


@kopf.on.resume('', 'v1', 'persistentvolumeclaims',
    when=filter_create_dataset)
@kopf.on.create('', 'v1', 'persistentvolumeclaims',
    when=filter_create_dataset)
@kopf.on.update('', 'v1', 'persistentvolumeclaims',
    when=filter_create_dataset)
@annotate_results
def create_dataset(name, namespace, body, meta, spec, status, patch, **_):
    """Schedule a pod that creates the zfs dataset.
    """
    #log.debug(f'create_dataset: status: {status}')
    #log.debug(f'create_dataset: body: {body}')
    #log.debug(f'STORAGE_CLASS_PARAMETERS: {STORAGE_CLASS_PARAMETERS}')
    log.info('Creating zfs dataset for pvc: %s', name)
    storage_class_name = spec['storageClassName']
    storage_class = CONFIG.storage_classes[storage_class_name]

    action = 'create'
    pv_name = f"pvc-{meta['uid']}"
    pod_name = f"{pv_name}-{action}"

    result = {
        'pv_name': pv_name,
        'pod_name': pod_name,
    }

    storage_class_mode = storage_class.parameters.get('mode', 'local')
    if storage_class_mode == storage_class.MODE_LOCAL:
        selected_node = meta.annotations['volume.kubernetes.io/selected-node']
        # TODO: get/check parent dataset override from config
        parent_dataset = CONFIG.default_parent_dataset
        dataset_name = os.path.join(parent_dataset, pv_name)
        mount_point = os.path.join(CONFIG.dataset_mount_dir, pv_name)
        result['dataset_name'] = dataset_name
        result['mount_point'] = mount_point
        result['selected_node'] = selected_node

        pod_args = ['dataset', 'create']

        try:
            storage = spec['resources']['requests']['storage']
            if storage[-1:] == 'i':
                # Turn e.g. Gi into Gib so that bitmath understands it.
                storage = storage + 'b'
            quota = int(bitmath.parse_string(storage).bytes)
            pod_args.extend(['--quota', str(quota)])
        except KeyError as e:
            log.error(e)

        pod_args.append(dataset_name)
        pod_args.append(mount_point)

    #elif storage_class_mode == storage_class.MODE_NFS:
    #   - get nfs server node name from config, schedule create pod create
    #   - setup nfs export, if at all possible using zfs property instead of exportfs
    else:
        raise kopf.HandlerFatalError(f'Unsupported storage class mode: {storage_class_mode}')

    log.info('pod_args: %s', pod_args)

    #data = get_example_pod(pod_name, selected_node)
    data = get_dataset_pod(pod_name, selected_node, pod_args)

    # Make the pod a child of the PVC.
    kopf.adopt(data)

    # Label the pod for filtering in the on.event handler.
    kopf.label(data, {'zfs-provisioner/action': action})

    # Create the pod.
    api = kubernetes.client.CoreV1Api()
    obj = api.create_namespaced_pod(
        body=data,
        namespace=namespace,
    )

    # Set the initial dataset creation phase to that of the newly created pod.
    annotation = CONFIG.dataset_phase_annotations[action]
    patch.metadata.annotations[annotation] = obj.status.phase

    # Remember the pods name so that the other handler can delete it after the
    # dataset has been created.
    result['phase'] = obj.status.phase
    return result


pvc_resource = resources.Resource(group='', version='v1', plural='persistentvolumeclaims')

@kopf.on.event('', 'v1', 'pods', labels={'zfs-provisioner/action': 'create'})
async def dataset_create_pod_event(name, event, body, meta, status, namespace, **kwargs):
    """Watch our dataset management pods for success or failures.
    """
    log.debug('pod_event: event: %s', event)
    #log.debug(f'pod_event: body: {body}')

    # Only care about changes to existing pods.
    if event['type'] == 'MODIFIED':

        phase = status['phase']
        action = meta.labels['zfs-provisioner/action']
        annotation = CONFIG.dataset_phase_annotations[action]

        log.debug('pod_event: %s: %s', action, phase)

        if phase in ('Succeeded', 'Failed'):
            log.info('dataset %s: %s -> %s', name, action, phase)
            patch = patches.Patch()
            patch.metadata.annotations[annotation] = phase

            for owner in meta.get('ownerReferences', []):
                if owner['kind'] == 'PersistentVolumeClaim':
                    await patching.patch_obj(
                        resource=pvc_resource,
                        patch=patch,
                        name=owner['name'],
                        namespace=owner.get('namespace', namespace),
                    )

            # All done. Delete the pod.
            # TODO: in case of failure get errors and store them somewhere?
            await kubernetes_asyncio.config.load_kube_config()
            async with kubernetes_asyncio.client.ApiClient() as api:
                v1 = kubernetes_asyncio.client.CoreV1Api(api)
                log.info('Deleting dataset creation pod: %s', name)
                await v1.delete_namespaced_pod(name, namespace)


def filter_create_pv(body, meta, spec, status, **_):
    """Filter function for resume, create and update handlers
    that filters out the PVCs for which the PV can be created.
    """
    # Only care about PVCs that are in state Pending.
    if status.get('phase', None) != 'Pending':
        return False

    action = 'create'
    annotation = CONFIG.dataset_phase_annotations[action]

    dataset_phase = meta.annotations.get(annotation, 'Pending')

    # Only care about PVCs for which we have tried to create the dataset.
    if dataset_phase not in ('Succeeded', 'Failed'):
        return False

    return True


@kopf.on.update('', 'v1', 'persistentvolumeclaims',
    when=filter_create_pv)
@annotate_results
def create_pv(name, namespace, body, meta, spec, patch, results, **kwargs):
    """Create the PV to fullfill this PVC.
    """
    action = 'create'
    annotation = CONFIG.dataset_phase_annotations[action]

    dataset_phase = meta.annotations.get(annotation, 'Pending')
    if dataset_phase == 'Pending':
        # Should never happen. Already handled by filter.
        raise kopf.TemporaryError('Dataset has not been created yet.', delay=10)
    elif dataset_phase == 'Failed':
        raise kopf.HandlerFatalError('Failed to create dataset.')
    elif dataset_phase == 'Succeeded':
        api = kubernetes.client.CoreV1Api()

        create_dataset_results = results.get('create_dataset', {})

        storage_class_name = spec['storageClassName']
        storage_class = CONFIG.storage_classes[storage_class_name]
        selected_node = meta.annotations['volume.kubernetes.io/selected-node']

        template = get_template('pvc.yaml')
        text = template.format(
            provisioner_name=storage_class.provisioner,
            pv_name=create_dataset_results['pv_name'],
            access_mode=spec['accessModes'][0],
            storage=spec['resources']['requests']['storage'],
            pvc_name=name,
            pvc_namespace=namespace,
            local_path=create_dataset_results['mount_point'],
            selected_node_name=selected_node,
            storage_class_name=storage_class_name,
            volume_mode=spec['volumeMode'],
            reclaim_policy=storage_class.reclaimPolicy,
        )
        data = yaml.safe_load(text)

        log.info('Creating PV for pvc: %s on node: %s', name, selected_node)
        api.create_persistent_volume(body=data)


def filter_delete_dataset(body, meta, spec, status, **_):
    """Filter function for delete handlers that filters out the PVCs for which
    the dataset deletion process can be started.
    """
    # Only care about PVCs that are in state Pending.
    # TODO: in which phases can we safely delete a dataset?
    #if status.get('phase', None) != 'Pending':
    #    return False

    # Only care about PVCs that we are not already working on.
    if CONFIG.dataset_phase_annotations['delete'] in meta.annotations:
        return False

    handle_it = False
    try:
        # Only care about PVCs that have a storage class that we are responsible for.
        storage_class_name = spec['storageClassName']
        storage_class = CONFIG.storage_classes[storage_class_name]
        handle_it = True

        # Check storage class specific settings.
        if storage_class.reclaimPolicy == storage_class.RECLAIM_POLICY_DELETE:
            handle_it = True
    except KeyError:
        handle_it = False
    return handle_it

#@kopf.on.delete('', 'v1', 'persistentvolumeclaims',
#    when=filter_delete_dataset)
#@annotate_results
#def testing_delete_handler(name, namespace, body, meta, spec, status, patch, results, **_):
#    """Schedule a pod that deletes the zfs dataset.
#    """
#    log.info('testing_delete_handler: %s', name)
#    import time
#    time.sleep(10)


@kopf.on.delete('', 'v1', 'persistentvolumeclaims',
    when=filter_delete_dataset)
@annotate_results
def delete_dataset(name, namespace, body, meta, spec, status, patch, results, **_):
    """Schedule a pod that deletes the zfs dataset.
    """
    log.info('Deleting zfs dataset for pvc: %s', name)

    action = 'delete'

    create_dataset_results = results['create_dataset']
    pv_name = create_dataset_results['pv_name']
    pod_name = f"{pv_name}-{action}"

    result = {
        'pv_name': pv_name,
        'pod_name': pod_name,
    }

    dataset_name = create_dataset_results['dataset_name']
    mount_point = create_dataset_results['mount_point']
    selected_node = create_dataset_results['selected_node']

    pod_args = ['dataset', 'destroy', dataset_name, mount_point]

    log.info('pod_args: %s', pod_args)

    data = get_dataset_pod(pod_name, selected_node, pod_args)

    # Make the pod a child of the PVC.
    #kopf.adopt(data)

    # Label the pod for filtering in the on.event handler.
    kopf.label(data, {'zfs-provisioner/action': action})

    # Create the pod.
    api = kubernetes.client.CoreV1Api()
    obj = api.create_namespaced_pod(
        body=data,
        namespace=namespace,
    )

    # Set the initial dataset creation phase to that of the newly created pod.
    annotation = CONFIG.dataset_phase_annotations[action]
    patch.metadata.annotations[annotation] = obj.status.phase

    # Delete the persistent volume.
    api.delete_persistent_volume(pv_name)

    # Remember the pods name so that the other handler can delete it after the
    # dataset has been created.
    result['phase'] = obj.status.phase
    return result


@kopf.on.event('', 'v1', 'pods', labels={'zfs-provisioner/action': 'delete'})
async def dataset_delete_pod_event(name, event, body, meta, status, namespace, **kwargs):
    """Watch our dataset deletion pods for success or failures.
    """
    log.debug('dataset_delete_pod_event: event: %s', event)
    #log.debug(f'pod_event: body: {body}')

    # Only care about changes to existing pods.
    if event['type'] == 'MODIFIED':

        phase = status['phase']
        action = meta.labels['zfs-provisioner/action']

        log.debug('pod_event: %s: %s', action, phase)

        if phase in ('Succeeded', 'Failed'):
            log.info('dataset %s: %s -> %s', name, action, phase)

            # All done. Delete the pod.
            # TODO: in case of failure get errors and store them somewhere?
            await kubernetes_asyncio.config.load_kube_config()
            async with kubernetes_asyncio.client.ApiClient() as api:
                v1 = kubernetes_asyncio.client.CoreV1Api(api)
                log.info('Deleting dataset creation pod: %s', name)
                await v1.delete_namespaced_pod(name, namespace)


@click.command()
@click.option('--verbose', '-v', 'log_level', flag_value='info', help='set log level to info', envvar='TENANTCTL_LOG_LEVEL')
@click.option('--debug', '-d', 'log_level', flag_value='debug', help='set log level to debug', envvar='TENANTCTL_LOG_LEVEL')
def main(log_level):
    """Run this module with logging better suited to local development
    then what `kopf run` offers.
    """
    logging.basicConfig(level=logging.ERROR, format='%(levelname)s: %(message)s', stream=sys.stderr)

    global log
    log = logging.getLogger(__name__)
    if log_level:
        log.setLevel(getattr(logging, log_level.upper()))
        logging.getLogger('kopf').setLevel(getattr(logging, log_level.upper()))

    log.debug('Starting kopf ...')
    loop = asyncio.get_event_loop()
    tasks = loop.run_until_complete(kopf.spawn_tasks())
    loop.run_until_complete(kopf.run_tasks(tasks))


if __name__ == '__main__':
    main()
