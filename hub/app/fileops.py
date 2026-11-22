"""文件边界和不覆盖的保存位置。"""
import os
import re
import time
import uuid


def inside(path, root):
    p, r = os.path.normcase(os.path.realpath(path)), os.path.normcase(os.path.realpath(root))
    return p == r or p.startswith(r.rstrip(os.sep) + os.sep)


def component(name):
    if (not isinstance(name, str) or not name or name in {".", ".."} or
            re.search(r'[\\/:*?"<>|\x00-\x1f]', name) or name.endswith((" ", ".")) or
            re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', name)):
        raise ValueError("无效的文件名")
    return name


def stamp():
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex


def recycle(path, trash):
    """每次独立目录，保留原文件名；永不覆盖另一次回收。"""
    import shutil
    folder = os.path.join(trash, stamp())
    os.makedirs(folder, exist_ok=False)
    return shutil.move(path, os.path.join(folder, os.path.basename(path)))


def remove(path):
    """真删，不进回收站（用户在网页上点了“彻底删除”才用；Agent 的工具只会移到回收站）。
    文件、链接直接删，文件夹整棵删；只读文件先去掉只读再删。返回删掉的字节数（删之前数的）。"""
    import shutil
    import stat

    def unlock(fn, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        fn(p)

    if os.path.islink(path) or not os.path.isdir(path):
        size = os.lstat(path).st_size
        try:
            os.remove(path)
        except PermissionError:
            os.chmod(path, stat.S_IWRITE)
            os.remove(path)
        return size
    size = 0
    for d, _dirs, files in os.walk(path):
        for f in files:
            try:
                size += os.lstat(os.path.join(d, f)).st_size
            except OSError:
                pass
    shutil.rmtree(path, onerror=unlock)
    return size


def sensitive(path):
    from .config import KEY_PATH
    return os.path.normcase(os.path.realpath(path)) == os.path.normcase(os.path.realpath(KEY_PATH))
