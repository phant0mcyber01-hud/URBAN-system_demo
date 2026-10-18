# The build and test environment of URBAN Edge OS. Only a tool: the OS itself does not use Docker.
# Debian bookworm, the same release the image is made of. swtpm: a software TPM for the VMs (the data partition
# is encrypted with a key in the TPM). The demo's screens use os/demo/screen.py and noVNC from web/node_modules.
FROM debian:bookworm

# retries and short timeouts: a stalled mirror connection must not hang the build for hours
RUN apt-get -o Acquire::Retries=5 -o Acquire::http::Timeout=30 update \
    && apt-get -o Acquire::Retries=5 -o Acquire::http::Timeout=30 install -y --no-install-recommends \
        debootstrap squashfs-tools dosfstools mtools e2fsprogs fdisk \
        grub-efi-amd64-bin grub-common rauc openssl \
        qemu-system-x86 qemu-utils ovmf openssh-client swtpm swtpm-tools \
        python3 ca-certificates xz-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /work
