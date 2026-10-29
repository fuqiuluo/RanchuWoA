"""gsi_undef_types.py — GSI 实验：找出 GSI 的各 *_contexts 文件引用、但最终策略里没有定义的 SELinux 类型。

最终策略 = GSI 的 plat/system_ext 策略 + 映射文件 + 我们 vendor 的策略。属性上下文里有未定义类型时 init 初始化属性区
失败（invalid_context）直接重启；文件/服务/应用上下文里的未定义类型会让对应对象打不上标签。结果补到
gfx/vendor-overlay/gsi/gsi_sepolicy.cil（只需类型声明）。

用法: py scripts/gsi_undef_types.py <cil 文件...> -- <contexts 文件...>
  cil：GSI 的 plat_sepolicy(_debug).cil、system_ext_sepolicy(_debug).cil、mapping/<vendor 版本>.cil，
       vendor 的 vendor_sepolicy.cil、plat_pub_versioned.cil（都可用 scripts/ext4tool.py get 从镜像里取）
  contexts：GSI 与 vendor 的 *_property_contexts、*_file_contexts、*_service_contexts、*_seapp_contexts 等
输出：每行 "<类型> <引用它的文件>"。
"""
import re
import sys


def main():
    args = sys.argv[1:]
    if "--" not in args:
        sys.exit(__doc__)
    sep = args.index("--")
    cils, ctxs = args[:sep], args[sep + 1:]

    defined = set()
    for p in cils:
        text = open(p, encoding="utf-8", errors="replace").read()
        defined.update(re.findall(r"\((?:type|typeattribute)\s+([A-Za-z0-9_]+)\)", text))

    refs = {}
    for p in ctxs:
        name = p.replace("\\", "/").split("/")[-1]
        for line in open(p, encoding="utf-8", errors="replace"):
            line = line.split("#", 1)[0]
            types = re.findall(r"u:object_r:([A-Za-z0-9_]+):s0", line)
            types += re.findall(r"\b(?:domain|type)=([A-Za-z0-9_]+)", line)     # seapp_contexts
            for t in types:
                refs.setdefault(t, set()).add(name)

    missing = sorted(t for t in refs if t not in defined)
    for t in missing:
        print(t, ",".join(sorted(refs[t])))
    print(f"# {len(missing)} 个未定义类型", file=sys.stderr)


if __name__ == "__main__":
    main()
