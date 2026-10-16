# The build and test environment of URBAN Edge OS. Only a tool: the OS itself does not use Docker.
# Debian bookworm, the same release the image is made of.
FROM debian:bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
        debootstrap squashfs-tools dosfstools mtools e2fsprogs fdisk \
        grub-efi-amd64-bin grub-common rauc openssl \
        qemu-system-x86 qemu-utils ovmf openssh-client \
        python3 ca-certificates xz-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /work
