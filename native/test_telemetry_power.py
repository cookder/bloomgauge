"""Exercise the telemetry helper's power fallback without depending on Mac sensors."""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipUnless(shutil.which('cc'), 'C compiler required')
class TelemetryPowerTests(unittest.TestCase):
    def test_sensor_priority_and_invalid_readings(self):
        source = (Path(__file__).parent / 'telemetry.m').read_text()
        function = source.split('// BEGIN systemPower\n', 1)[1].split('// END systemPower', 1)[0]
        harness = r"""
#include <assert.h>
#include <math.h>
#include <string.h>
static double readings[3];
static int calls;
static double sensor(const char *key) {
    calls++;
    if (!strcmp(key, "PSTR")) return readings[0];
    if (!strcmp(key, "PDTR")) return readings[1];
    assert(!strcmp(key, "PD0R"));
    return readings[2];
}
"""
        checks = r"""
static void check(double pstr, double pdtr, double pd0r, double expected, int count) {
    readings[0]=pstr; readings[1]=pdtr; readings[2]=pd0r; calls=0;
    double result=systemPower();
    assert(isnan(expected) ? isnan(result) : result==expected);
    assert(calls==count);
}
int main(void) {
    check(42, 70, 80, 42, 1);
    check(0, 70, 80, 70, 2);
    check(0, 0, 234, 234, 3);
    check(NAN, NAN, 234, 234, 3);
    check(-1, INFINITY, 234, 234, 3);
    check(1001, -INFINITY, 234, 234, 3);
    check(1000, 70, 80, 1000, 1);
    check(0, 0, 0, NAN, 3);
    check(NAN, -1, 1001, NAN, 3);
    return 0;
}
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / 'power.c').write_text(harness + function + checks)
            subprocess.run(
                [
                    'cc',
                    '-std=c11',
                    '-Wall',
                    '-Wextra',
                    '-Werror',
                    str(path / 'power.c'),
                    '-o',
                    str(path / 'power'),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run([str(path / 'power')], check=True, capture_output=True, text=True)
