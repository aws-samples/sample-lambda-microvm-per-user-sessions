# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
FROM public.ecr.aws/lambda/microvms:al2023-minimal

# openvscode-server (MIT, https://github.com/gitpod-io/openvscode-server).
# Pinned by version and SHA-256 so every image build gets the same bits.
ARG OVS_VERSION=1.109.5
ARG OVS_SHA256=36d9c14036489b63de84ebace837fcacf7e60e669a0dc715802c5443684ea4dc

RUN dnf install -y tar gzip git python3 shadow-utils hostname procps-ng which less && dnf clean all

RUN curl -fsSL -o /tmp/ovs.tgz \
      "https://github.com/gitpod-io/openvscode-server/releases/download/openvscode-server-v${OVS_VERSION}/openvscode-server-v${OVS_VERSION}-linux-arm64.tar.gz" \
 && echo "${OVS_SHA256}  /tmp/ovs.tgz" | sha256sum -c - \
 && mkdir -p /opt/openvscode-server \
 && tar xzf /tmp/ovs.tgz -C /opt/openvscode-server --strip-components=1 \
 && rm /tmp/ovs.tgz

# VS Code runs as an unprivileged user with its own home and workspace folder.
RUN useradd --create-home --shell /bin/bash coder
COPY --chown=coder:coder workspace/ /home/coder/workspace/
# Each workspace belongs to one user who controls everything in it, so VS Code's
# workspace-trust prompt (Restricted Mode) is turned off. See README > Security.
COPY --chown=coder:coder settings/machine-settings.json /home/coder/.openvscode-server/data/Machine/settings.json
COPY hooks.py entrypoint.sh /opt/microvm/
RUN chmod 0755 /opt/microvm/entrypoint.sh

USER coder
WORKDIR /home/coder

# 8080: VS Code (the MicroVM proxy's default target port). 9000: lifecycle hooks.
EXPOSE 8080 9000

CMD ["/opt/microvm/entrypoint.sh"]
