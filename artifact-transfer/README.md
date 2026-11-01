# Read-only artifact transfer

`nova-artifact-read.py` is the SSH forced-command helper used to transfer Nova reference recordings and screenshots between the Macs. This is a source copy of the installed helper, not an installation action.

The helper accepts only:

- `ping`: return the service identity and access mode.
- `stat RELATIVE_PATH`: return the file size and SHA-256.
- `fetch RELATIVE_PATH`: stream the file bytes.

Paths must be relative to the configured `ROOT`. Empty components, `.` and `..` are refused. Each component is opened relative to its parent descriptor without following symlinks, and only regular files can be read. Incoming commands are parsed with `shlex`; no shell command is executed.

## Installation

Review and set the `ROOT` constant for the intake directory on the artifact host. Install the helper outside that directory, owned by the SSH account and not writable by other users. Configure a dedicated SSH public key with a forced command invoking this helper, a restriction to the worker's source address, and no forwarding, agent forwarding, PTY or user startup files. Use a pinned host key, `BatchMode yes` and a dedicated identity on the client. Do not store the private key, live `authorized_keys`, client SSH configuration or artifact files in this repository.

The current deployment uses the `nova-artifacts-mac` SSH alias and the intake directory under `Documents/ChatGPT/exports/nova-task-intake`. For example:

```sh
ssh nova-artifacts-mac ping
ssh nova-artifacts-mac stat '20261031/example/reference.mov'
ssh nova-artifacts-mac fetch '20261031/example/reference.mov' > reference.mov.partial
```

Check the downloaded size and digest against `stat` before promoting the temporary file. This route does not provide arbitrary shell, SCP or SFTP access. Preserve the installed helper and existing SSH authorization when updating its source copy.
