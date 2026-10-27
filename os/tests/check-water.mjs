// G4: a pump station on URBAN OS (MODULE=water, pump-S-01) reading a fake PLC over Modbus TCP, enrolled at a control
// centre (role center, no second factor for the test's admin): the centre shows the PLC's readings, a pump fault and a
// low pressure set in the PLC become alerts and clear, a PLC that stops answering becomes a source alert while the
// station stays online, the centre has no command route, and the PLC was sent nothing but Modbus reads (01-04).
import { build, fail, scenario } from "./lib.mjs";

build("1", { image: true });
if (scenario("water") !== 0) fail("water scenario");
