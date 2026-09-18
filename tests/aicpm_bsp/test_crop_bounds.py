"""Exercise production selection/get-format callbacks against CIF's bounds contract."""
import pathlib
import re
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]


def function(source, name):
    start = re.search(r'static int ' + name + r'\s*\(', source).start()
    end = source.index('{', start) + 1
    depth = 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


class CropBoundsTest(unittest.TestCase):
    def test_cif_bounds_fit_output_for_active_and_try_formats(self):
        source = (ROOT / 'sysdrv/source/kernel/drivers/media/i2c/ov5647.c').read_text()
        harness = r'''
#include <assert.h>
#include <errno.h>
#define V4L2_SUBDEV_FORMAT_TRY 0
#define V4L2_SUBDEV_FORMAT_ACTIVE 1
#define V4L2_SEL_TGT_CROP 0
#define V4L2_SEL_TGT_NATIVE_SIZE 3
#define V4L2_SEL_TGT_CROP_DEFAULT 1
#define V4L2_SEL_TGT_CROP_BOUNDS 2
#define OV5647_NATIVE_WIDTH 2624
#define OV5647_NATIVE_HEIGHT 1956
#define OV5647_PIXEL_ARRAY_LEFT 16
#define OV5647_PIXEL_ARRAY_TOP 16
#define OV5647_PIXEL_ARRAY_WIDTH 2592
#define OV5647_PIXEL_ARRAY_HEIGHT 1944
struct v4l2_rect { int left, top, width, height; };
struct v4l2_mbus_framefmt { unsigned int width, height; };
struct v4l2_subdev_format { unsigned int which, pad; struct v4l2_mbus_framefmt format; };
struct v4l2_subdev_selection { unsigned int which, pad, target; struct v4l2_rect r; };
struct v4l2_subdev { int unused; };
struct v4l2_subdev_pad_config { struct v4l2_mbus_framefmt format; };
struct ov5647_mode { struct v4l2_mbus_framefmt format; };
struct mutex { int held; };
struct ov5647 { struct v4l2_subdev sd; struct mutex lock; const struct ov5647_mode *mode; };
static struct ov5647 *to_sensor(struct v4l2_subdev *sd) { return (struct ov5647 *)sd; }
static void mutex_lock(struct mutex *m) { assert(!m->held); m->held=1; }
static void mutex_unlock(struct mutex *m) { assert(m->held); m->held=0; }
static struct v4l2_mbus_framefmt *v4l2_subdev_get_try_format(struct v4l2_subdev *sd, struct v4l2_subdev_pad_config *cfg, unsigned int pad) { return &cfg->format; }
static struct v4l2_rect *__ov5647_get_pad_crop(struct ov5647 *s, struct v4l2_subdev_pad_config *c, unsigned int p, unsigned int w) { static struct v4l2_rect crop={32,16,2560,1920}; return &crop; }
'''
        harness += function(source, 'ov5647_get_pad_fmt')
        harness += function(source, 'ov5647_get_selection')
        harness += r'''
int main(void) {
 const struct ov5647_mode modes[] = {{{2592,1944}},{{1920,1080}},{{1296,972}},{{640,480}}};
 struct ov5647 s = {0}; struct v4l2_subdev_pad_config cfg = {{1920,1080}};
 struct v4l2_subdev_selection sel = {.which=1,.pad=0,.target=2};
 for (int i=0;i<4;i++) {
  s.mode=&modes[i];
  assert(ov5647_get_selection(&s.sd,&cfg,&sel)==0);
  assert(sel.r.left==0 && sel.r.top==0);
  assert(sel.r.width==modes[i].format.width && sel.r.height==modes[i].format.height);
  assert(s.lock.held==0);
 }
 sel.which=0;
 assert(ov5647_get_selection(&s.sd,&cfg,&sel)==0);
 assert(sel.r.left==0 && sel.r.top==0 && sel.r.width==1920 && sel.r.height==1080);
 sel.pad=1; assert(ov5647_get_selection(&s.sd,&cfg,&sel)==-EINVAL);
 sel.pad=0; sel.which=1; sel.target=3;
 assert(ov5647_get_selection(&s.sd,&cfg,&sel)==0);
 assert(sel.r.width==2624 && sel.r.height==1956);
 return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            cfile = pathlib.Path(directory) / 'crop.c'
            binary = pathlib.Path(directory) / 'crop'
            cfile.write_text(harness)
            subprocess.run(['cc', '-std=c99', str(cfile), '-o', str(binary)], check=True)
            result = subprocess.run([str(binary)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
