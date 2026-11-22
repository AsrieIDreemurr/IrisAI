"""Windows 作业对象：失败即报错，不让调用方误以为进程已绑定。"""
import ctypes
from ctypes import wintypes as w


class Job:
    def __init__(self, pid):
        k = self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        for name, args, result in (
            ("CreateJobObjectW", [ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
            ("SetInformationJobObject", [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
            ("OpenProcess", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            ("AssignProcessToJobObject", [w.HANDLE, w.HANDLE], w.BOOL),
            ("CloseHandle", [w.HANDLE], w.BOOL),
        ):
            fn = getattr(k, name)
            fn.argtypes, fn.restype = args, result

        class Limits(ctypes.Structure):
            _fields_ = [("per_process", ctypes.c_int64), ("per_job", ctypes.c_int64), ("flags", w.DWORD),
                        ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t), ("active", w.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD)]

        class Extended(ctypes.Structure):
            _fields_ = [("basic", Limits), ("io", ctypes.c_uint64 * 6),
                        ("process_mem", ctypes.c_size_t), ("job_mem", ctypes.c_size_t),
                        ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]

        self.handle = k.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        process = None
        try:
            info = Extended()
            info.basic.flags = 0x2000
            if not k.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            process = k.OpenProcess(0x0101, False, pid)  # SET_QUOTA | TERMINATE
            if not process or not k.AssignProcessToJobObject(self.handle, process):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.close()
            raise
        finally:
            if process:
                k.CloseHandle(process)

    def close(self):
        if self.handle:
            self.k.CloseHandle(self.handle)
            self.handle = None
