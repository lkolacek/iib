# SPDX-License-Identifier: GPL-3.0-or-later
import copy
import fileinput
import json
import logging
import os
import subprocess
import tempfile
import textwrap
import time

from iib.exceptions import IIBError
from iib.workers.api_utils import get_request, set_request_state, update_request
from iib.workers.config import get_worker_config
from iib.workers.tasks.celery import app
from iib.workers.tasks.general import failed_request_callback


__all__ = ['handle_add_request', 'opm_index_add']

log = logging.getLogger(__name__)


def _build_image(dockerfile_dir, request_id):
    """
    Build the index image.

    :param str dockerfile_dir: the path to the directory containing the data generated by the
        opm command.
    :param int request_id: the ID of the IIB build request
    :raises iib.exceptions.IIBError: if the build fails
    """
    destination = _get_local_pull_spec(request_id)
    log.info('Building the index image and tagging it as %s', destination)
    dockerfile_path = os.path.join(dockerfile_dir, 'index.Dockerfile')
    _run_cmd(
        ['podman', 'build', '-f', dockerfile_path, '-t', destination, '.'],
        {'cwd': dockerfile_dir},
        exc_msg=f'Failed to build the index image on the arch {_get_arch()}',
    )


def _cleanup():
    """
    Remove all existing container images on the host.

    This will ensure that the host will not run out of disk space due to stale data, and that
    all images referenced using floating tags will be up to date on the host.

    :raises iib.exceptions.IIBError: if the command to remove the images fails
    """
    log.info('Removing all existing container images')
    _run_cmd(
        ['podman', 'rmi', '--all', '--force'],
        exc_msg='Failed to remove the existing container images',
    )


def _create_and_push_manifest_list(request_id, arches):
    """
    Create and push the manifest list to the configured registry.

    :param int request_id: the ID of the IIB build request
    :param iter arches: an iterable of arches to create the manifest list for
    :return: the pull specification of the manifest list
    :rtype: str
    :raises iib.exceptions.IIBError: if creating or pushing the manifest list fails
    """
    conf = get_worker_config()
    output_pull_spec = conf['iib_image_push_template'].format(
        registry=conf['iib_registry'], request_id=request_id
    )
    log.info('Creating the manifest list %s', output_pull_spec)
    with tempfile.TemporaryDirectory(prefix='iib-') as temp_dir:
        manifest_yaml = os.path.abspath(os.path.join(temp_dir, 'manifest.yaml'))
        with open(manifest_yaml, 'w+') as manifest_yaml_f:
            manifest_yaml_f.write(
                textwrap.dedent(
                    f'''\
                    image: {output_pull_spec}
                    manifests:
                    '''
                )
            )
            for arch in sorted(arches):
                arch_pull_spec = _get_external_arch_pull_spec(request_id, arch)
                log.debug(
                    'Adding the manifest %s to the manifest list %s',
                    arch_pull_spec,
                    output_pull_spec,
                )
                manifest_yaml_f.write(
                    textwrap.dedent(
                        f'''\
                        - image: {arch_pull_spec}
                          platform:
                            architecture: {arch}
                            os: linux
                        '''
                    )
                )
            # Return back to the beginning of the file to output it to the logs
            manifest_yaml_f.seek(0)
            log.debug(
                'Created the manifest configuration with the following content:\n%s',
                manifest_yaml_f.read(),
            )

        username, password = conf['iib_registry_credentials'].split(':', 1)
        _run_cmd(
            [
                'manifest-tool',
                '--username',
                username,
                '--password',
                password,
                'push',
                'from-spec',
                manifest_yaml,
            ],
            exc_msg=f'Failed to push the manifest list to {output_pull_spec}',
        )

    return output_pull_spec


def _finish_request_post_build(request_id, arches):
    """
    Finish the request after the architecture builds have completed.

    This function was created so that code didn't need to be duplicated for the ``add`` and ``rm``
    request types.

    :param int request_id: the ID of the IIB build request
    :param set arches: the list of arches that were built as part of this request
    :raises iib.exceptions.IIBError: if the manifest list couldn't be created and pushed
    """
    set_request_state(request_id, 'in_progress', 'Creating the manifest list')
    output_pull_spec = _create_and_push_manifest_list(request_id, arches)
    payload = {
        'index_image': output_pull_spec,
        'state': 'complete',
        'state_reason': 'The request completed successfully',
    }
    update_request(request_id, payload, exc_msg='Failed setting the index image on the request')


def _fix_opm_path(dockerfile_dir):
    """
    Fix the path to /bin/opm in the generated Dockerfile.

    This is a workaround until https://github.com/operator-framework/operator-registry/pull/173 is
    released.

    :param str dockerfile_dir: the path to the directory containing the data generated by the
        opm command.
    """
    log.debug('Fixing the opm path in index.Dockerfile')
    dockerfile_path = os.path.join(dockerfile_dir, 'index.Dockerfile')
    # This modifies the index.Dockerfile file inplace. For every line, anything sent to stdout
    # will be used as the line in the file.
    for line in fileinput.input(dockerfile_path, inplace=True):
        # Keep the line the same unless it has '/build/bin/opm' in it. If it does, replace it with
        # '/bin/opm'.
        print(line.replace('/build/bin/opm', '/bin/opm'), end='')


def _get_arch():
    return get_worker_config()['iib_arch']


def _get_external_arch_pull_spec(request_id, arch=None, include_transport=False):
    """
    Get the pull specification of the single arch image in the external registry.

    :param int request_id: the ID of the IIB build request
    :param str arch: the desired architecture; if not specified, the configuration of
        ``iib_arch`` will be used
    :param bool include_transport: if true, `docker://` will be prefixed in the returned pull
        specification
    :return: the pull specification of the single arch image in the external registry
    :rtype: str
    """
    conf = get_worker_config()
    pull_spec = conf['iib_arch_image_push_template'].format(
        registry=conf['iib_registry'], request_id=request_id, arch=arch or conf['iib_arch']
    )
    if include_transport:
        return f'docker://{pull_spec}'
    return pull_spec


def _get_local_pull_spec(request_id):
    """
    Get the pull specification of the index image for this request.

    :return: the pull specification of the index image for this request.
    :rtype: str
    """
    return f'operator-registry-index:{request_id}'


def _get_image_arches(pull_spec):
    """
    Get the architectures this image was built for.

    :param str pull_spec: the pull specification to a v2 manifest list
    :return: a set of architectures of the images contained in the manifest list
    :rtype: set
    :raises iib.exceptions.IIBError: if the pull specification is not a v2 manifest list
    """
    log.debug('Get the available arches for %s', pull_spec)
    skopeo_raw = _skopeo_inspect(f'docker://{pull_spec}', '--raw')
    if skopeo_raw['mediaType'] != 'application/vnd.docker.distribution.manifest.list.v2+json':
        raise IIBError(f'The pull specification of {pull_spec} is not a v2 manifest list')

    arches = set()
    for manifest in skopeo_raw['manifests']:
        arches.add(manifest['platform']['architecture'])

    return arches


def _get_resolved_image(pull_spec):
    """
    Get the pull specification of the image using its digest.

    :param str pull_spec: the pull specification of the image to resolve
    :return: the resolved pull specification
    :rtype: str
    """
    log.debug('Resolving %s', pull_spec)
    skopeo_output = _skopeo_inspect(f'docker://{pull_spec}')
    pull_spec_resolved = f'{skopeo_output["Name"]}@{skopeo_output["Digest"]}'
    log.debug('%s resolved to %s', pull_spec, pull_spec_resolved)
    return pull_spec_resolved


def _prepare_request_for_build(binary_image, request_id, from_index=None, add_arches=None):
    """
    Prepare the request for the architecture specific builds.

    All information that was retrieved and/or calculated for the next steps in the build are
    returned as a dictionary.

    This function was created so that code didn't need to be duplicated for the ``add`` and ``rm``
    request types.

    :param str binary_image: the pull specification of the image where the opm binary gets copied
        from.
    :param int request_id: the ID of the IIB build request
    :param str from_index: the pull specification of the image containing the index that the index
        image build will be based from.
    :param list add_arches: the list of arches to build in addition to the arches ``from_index`` is
        currently built for; if ``from_index`` is ``None``, then this is used as the list of arches
        to build the index image for
    :return: a dictionary with the keys: arches, binary_image_resolved, and fom_index_resolved.
    :raises iib.exceptions.IIBError: if the image resolution fails or the architectures couldn't
        be detected.
    """
    set_request_state(request_id, 'in_progress', 'Resolving the images')

    if add_arches:
        arches = set(add_arches)
    else:
        arches = set()

    binary_image_resolved = _get_resolved_image(binary_image)
    binary_image_arches = _get_image_arches(binary_image_resolved)

    if from_index:
        from_index_resolved = _get_resolved_image(from_index)
        from_index_arches = _get_image_arches(from_index_resolved)
        arches = arches | from_index_arches
    else:
        from_index_resolved = None

    if not arches:
        raise IIBError('No arches were provided to build the index image')

    arches_str = ', '.join(sorted(arches))
    log.debug('Set to build the index image for the following arches: %s', arches_str)

    if not arches.issubset(binary_image_arches):
        raise IIBError(
            'The binary image is not available for the following arches: {}'.format(
                ', '.join(sorted(arches - binary_image_arches))
            )
        )

    conf = get_worker_config()
    if not arches.issubset(conf['iib_arches']):
        raise IIBError(
            'Building for the following requested arches is not supported: {}'.format(
                ','.join(sorted(arches - conf['iib_arches']))
            )
        )

    payload = {
        'binary_image_resolved': binary_image_resolved,
        'state': 'in_progress',
        'state_reason': f'Scheduling index image builds for the following arches: {arches_str}',
    }
    if from_index_resolved:
        payload['from_index_resolved'] = from_index_resolved
    exc_msg = 'Failed setting the resolved images on the request'
    update_request(request_id, payload, exc_msg)

    return {
        'arches': arches,
        'binary_image_resolved': binary_image_resolved,
        'from_index_resolved': from_index_resolved,
    }


def _poll_request(request_id, arches):
    """
    Poll the IIB API until the request has all the arches built or has failed.

    This function was created so that code didn't need to be duplicated for the ``add`` and ``rm``
    request types.

    :param int request_id: the ID of the IIB build request
    :param list arches: the list of arches to wait for to be built
    :return: True if all the architectures were built and False if the request left the
        ``in_progress`` state (e.g. it was set to ``failed``)
    :rtype: bool
    :raises iib.exceptions.IIBError: if the request to the IIB API fails after a number of retries
    """
    arches_remaining = copy.copy(arches)
    conf = get_worker_config()
    while True:
        log.info('Sleeping for %s seconds', conf['iib_poll_api_frequency'])
        time.sleep(conf['iib_poll_api_frequency'])
        log.info(
            'Polling the API to see if the image builds for the following arches have '
            'completed: %s',
            ', '.join(arches_remaining),
        )

        request = get_request(request_id)
        # If the request failed, there is no chance the architectures will be set, and the
        # manifest list should not be created. Additionally, we want to guard against a task
        # for the same request getting executed twice. So if the request is in the complete
        # state, we don't want to push manifest list again.
        if request['state'] != 'in_progress':
            return False

        arches_remaining = arches_remaining - set(request['arches'])
        if not arches_remaining:
            log.info('All the underlying builds have completed')
            return True


def _push_arch_image(request_id):
    """
    Push the single arch index image to the configured registry.

    :param int request_id: the ID of the IIB build request
    :raises iib.exceptions.IIBError: if the push fails
    """
    source = _get_local_pull_spec(request_id)
    destination = _get_external_arch_pull_spec(request_id, include_transport=True)
    log.info('Pushing the index image %s to %s', source, destination)
    conf = get_worker_config()
    _run_cmd(
        ['podman', 'push', '-q', source, destination, '--creds', conf['iib_registry_credentials']],
        exc_msg=f'Failed to push the index image to {destination} on the arch {_get_arch()}',
    )


def _skopeo_inspect(*args, use_creds=False):
    """
    Wrap the ``skopeo inspect`` command.

    :param *args: any arguments to pass to ``skopeo inspect``
    :param bool use_creds: if true, the registry credentials in the configuration will be used
    :return: a dictionary of the JSON output from the skopeo inspect command
    :rtype: dict
    :raises iib.exceptions.IIBError: if the command fails
    """
    exc_msg = None
    for arg in args:
        if arg.startswith('docker://'):
            exc_msg = f'Failed to inspect {arg}. Make sure it exists and is accessible to IIB.'
            break

    cmd = ['skopeo', 'inspect'] + list(args)
    if use_creds:
        conf = get_worker_config()
        cmd.extend(['--creds', conf['iib_registry_credentials']])
    return json.loads(_run_cmd(cmd, exc_msg=exc_msg))


@app.task
def handle_add_request(bundles, binary_image, request_id, from_index=None, add_arches=None):
    """
    Coordinate the the work needed to build the index image with the input bundles.

    :param list bundles: a list of strings representing the pull specifications of the bundles to
        add to the index image being built.
    :param str binary_image: the pull specification of the image where the opm binary gets copied
        from.
    :param int request_id: the ID of the IIB build request
    :param str from_index: the pull specification of the image containing the index that the index
        image build will be based from.
    :param list add_arches: the list of arches to build in addition to the arches ``from_index`` is
        currently built for; if ``from_index`` is ``None``, then this is used as the list of arches
        to build the index image for
    :raises iib.exceptions.IIBError: if the index image build fails.
    """
    prebuild_info = _prepare_request_for_build(binary_image, request_id, from_index, add_arches)

    error_callback = failed_request_callback.s(request_id=request_id)
    arches = prebuild_info['arches']
    for arch in sorted(arches):
        # The bundles are not resolved since these are stable tags, and references
        # to a bundle image using a digest fails when using the opm command.
        opm_index_add.apply_async(
            args=[
                bundles,
                prebuild_info['binary_image_resolved'],
                request_id,
                prebuild_info['from_index_resolved'],
            ],
            link_error=error_callback,
            queue=f'iib_{arch}',
            routing_key=f'iib_{arch}',
        )

    if not _poll_request(request_id, arches):
        log.error('Not finishing the request since one of the underlying builds failed')
        return

    _finish_request_post_build(request_id, arches)


@app.task
def opm_index_add(bundles, binary_image, request_id, from_index=None):
    """
    Build and push an operator index image for a specific architecture with the input bundles.

    :param list bundles: a list of strings representing the pull specifications of the bundles to
        add to the index image being built.
    :param str binary_image: the pull specification of the image where the opm binary gets copied
        from.
    :param int request_id: the ID of the IIB build request
    :param str from_index: the pull specification of the image containing the index that the index
        image build will be based from.
    :raises iib.exceptions.IIBError: if the index image build fails.
    """
    request = get_request(request_id)
    if request['state'] == 'failed':
        set_request_state(
            request_id,
            'failed',
            f'Not building for the arch {_get_arch()} since the request has already failed',
        )
        return

    _cleanup()
    with tempfile.TemporaryDirectory(prefix='iib-') as temp_dir:
        # TODO: Once https://github.com/operator-framework/operator-registry/pull/173 is
        # released, opm can just call podman directly for us.
        cmd = [
            'opm',
            'index',
            'add',
            '--generate',
            '--bundles',
            ','.join(bundles),
            '--binary-image',
            binary_image,
        ]

        log.info(
            'Generating the database file with the following bundle(s): %s', ', '.join(bundles)
        )
        if from_index:
            log.info('Using the existing database from %s', from_index)
            cmd.extend(['--from-index', from_index])

        _run_cmd(
            cmd,
            {'cwd': temp_dir},
            exc_msg=f'Failed to add the bundles to the index image on the arch {_get_arch()}',
        )

        _fix_opm_path(temp_dir)
        _build_image(temp_dir, request_id)

    _push_arch_image(request_id)
    update_request(
        request_id, {'arches': [_get_arch()]}, exc_msg=f'Failed adding the arch {_get_arch()}'
    )


def _run_cmd(cmd, params=None, exc_msg=None):
    """
    Run the given command with the provided parameters.

    :param iter cmd: iterable representing the command to be executed
    :param dict params: keyword parameters for command execution
    :param str exc_msg: an optional exception message when the command fails
    :return: the command output
    :rtype: str
    :raises iib.exceptions.IIBError: if the command fails
    """
    if not params:
        params = {}
    params.setdefault('universal_newlines', True)
    params.setdefault('encoding', 'utf-8')
    params.setdefault('stderr', subprocess.PIPE)
    params.setdefault('stdout', subprocess.PIPE)

    response = subprocess.run(cmd, **params)

    if response.returncode != 0:
        conf = get_worker_config()
        _, password = conf['iib_registry_credentials'].split(':', 1)
        sanitized_cmd = copy.copy(cmd)
        for i, arg in enumerate(cmd):
            if arg in (conf['iib_registry_credentials'], password):
                sanitized_cmd[i] = '********'
        log.error('The command "%s" failed with: %s', ' '.join(sanitized_cmd), response.stderr)
        raise IIBError(exc_msg or 'An unexpected error occurred')

    return response.stdout
