// G5 / step 7: partitions only from the disk the unit booted from. A stranger's USB stick with GPT partitions named esp,
// rootfs-a, rootfs-b and urban-data (ext4 labelled urban-data) is attached to a unit from power-on, Secure Boot on: the
// unit boots, mounts and updates (version 2 into slot B) only from its own disk, its /dev/disk/urban links point to that
// disk, and the stick is byte for byte unchanged. The control: the old by-partlabel links do resolve to the stick.
// Emulated: a long run (two boots and an update).
import { build, fail, scenario } from "./lib.mjs";

build("1", { image: true });
build("2");
if (scenario("bootdisk") !== 0) fail("boot disk scenario");
