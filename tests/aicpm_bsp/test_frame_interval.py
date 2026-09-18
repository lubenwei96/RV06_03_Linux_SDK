"""Compile and call the production video-op registration on host fixtures.

Catches missing registration, stale/hardcoded mode intervals and invalid-pad
acceptance. The real getter is compiled, not replaced by a Python model.
"""
import pathlib
import re
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
DRIVER = ROOT / 'sysdrv/source/kernel/drivers/media/i2c/ov5647.c'


class FrameIntervalTest(unittest.TestCase):
    def test_registered_query_tracks_mode_and_rejects_other_pads(self):
        source = DRIVER.read_text()
        ops = re.search(r'static const struct v4l2_subdev_video_ops '
                        r'ov5647_subdev_video_ops\s*=\s*\{.*?\};', source, re.S).group()
        registration = re.search(r'\.g_frame_interval\s*=\s*(\w+)', ops)
        getter = ''
        if registration:
            name = registration.group(1)
            start = re.search(r'static int ' + name + r'\s*\(', source).start()
            brace = source.index('{', start)
            depth = 1
            end = brace + 1
            while depth:
                depth += (source[end] == '{') - (source[end] == '}')
                end += 1
            getter = source[start:end]
        harness = r'''
#include <assert.h>
#include <errno.h>
#include <stddef.h>
struct v4l2_fract { unsigned int numerator, denominator; };
struct v4l2_subdev { int unused; };
struct v4l2_subdev_frame_interval { unsigned int pad; struct v4l2_fract interval; };
struct ov5647_mode { struct v4l2_fract frame_interval; };
struct mutex { int held; };
struct ov5647 { struct v4l2_subdev sd; struct mutex lock; const struct ov5647_mode *mode; };
static struct ov5647 *to_sensor(struct v4l2_subdev *sd) { return (struct ov5647 *)sd; }
static void mutex_lock(struct mutex *m) { assert(!m->held); m->held = 1; }
static void mutex_unlock(struct mutex *m) { assert(m->held); m->held = 0; }
static int ov5647_s_stream(struct v4l2_subdev *sd, int enable) { return 0; }
struct v4l2_subdev_video_ops {
 int (*s_stream)(struct v4l2_subdev *, int);
 int (*g_frame_interval)(struct v4l2_subdev *, struct v4l2_subdev_frame_interval *);
};
'''
        harness += getter + '\n' + ops
        harness += r'''
int main(void) {
 const struct ov5647_mode modes[] = {{{1,15}}, {{1,30}}, {{1,30}}, {{1,60}}};
 struct ov5647 sensor = {0};
 struct v4l2_subdev_frame_interval fi = {0};
 assert(ov5647_subdev_video_ops.g_frame_interval != NULL);
 for (int i = 0; i < 4; ++i) {
  sensor.mode = &modes[i]; fi.pad = 0;
  fi.interval.numerator = 999; fi.interval.denominator = 999;
  assert(ov5647_subdev_video_ops.g_frame_interval(&sensor.sd, &fi) == 0);
  assert(fi.interval.numerator == 1);
  assert(fi.interval.denominator == (i == 0 ? 15 : i == 3 ? 60 : 30));
  assert(sensor.lock.held == 0);
 }
 fi.pad = 1; fi.interval.numerator = 123; fi.interval.denominator = 456;
 assert(ov5647_subdev_video_ops.g_frame_interval(&sensor.sd, &fi) == -EINVAL);
 assert(fi.interval.numerator == 123 && fi.interval.denominator == 456);
 assert(sensor.lock.held == 0);
 return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            cfile = pathlib.Path(directory) / 'interval.c'
            binary = pathlib.Path(directory) / 'interval'
            cfile.write_text(harness)
            subprocess.run(['cc', '-std=c99', str(cfile), '-o', str(binary)], check=True)
            result = subprocess.run([str(binary)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
