"""
测试脚本：验证钉钉AD同步工具拼音转换功能

验证内容：
1. pypinyin 依赖可用
2. chinese_to_pinyin() 函数转换正确
3. generate_sam_account_name() 优先级逻辑正确
4. sAMAccountName 长度限制（20字符截断）
5. 已有逻辑（clean_sam_account_name / escape_dn_value）不受影响

运行方式：
    C:\\Users\\Administrator\\.workbuddy\\binaries\\python\\versions\\3.13.12\\python.exe test_pinyin.py
"""

import sys
import os

# 确保能导入 ad_sync 模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ==================== 1. 依赖可用性测试 ====================
def test_dependency():
    print("\n========== 1. pypinyin 依赖可用性测试 ==========")
    passed = 0
    failed = 0
    try:
        import pypinyin
        from pypinyin import lazy_pinyin
        print(f"  [PASS] pypinyin 已安装，版本: {pypinyin.__version__}")
        passed += 1
    except ImportError as e:
        print(f"  [FAIL] pypinyin 导入失败: {e}")
        failed += 1
        return passed, failed

    # 验证 lazy_pinyin 基本可用
    try:
        result = lazy_pinyin("测试")
        assert result == ["ce", "shi"], f"lazy_pinyin 基本功能异常: {result}"
        print(f"  [PASS] lazy_pinyin 基本功能正常: '测试' -> {result}")
        passed += 1
    except Exception as e:
        print(f"  [FAIL] lazy_pinyin 基本功能异常: {e}")
        failed += 1

    return passed, failed


# ==================== 2. chinese_to_pinyin 函数测试 ====================
def test_chinese_to_pinyin():
    print("\n========== 2. chinese_to_pinyin() 函数测试 ==========")
    from ad_sync import chinese_to_pinyin
    passed = 0
    failed = 0
    failures = []

    # (输入, 期望输出, 说明)
    # 注意："Zhang San" 期望为 "Zhang San" —— 依据需求标注"英文保持不变"，
    # lazy_pinyin 对非中文字符原样保留（含大小写与空格）。
    # 规范化（小写/去空格）由下游 clean_sam_account_name 负责，端到端结果仍为 "zhangsan"。
    cases = [
        ("张三", "zhangsan", "纯中文姓名"),
        ("张三001", "zhangsan001", "中文+数字"),
        ("李四-测试", "lisi-ceshi", "中文+符号"),
        ("王五", "wangwu", "纯中文姓名"),
        ("诸葛亮", "zhugeliang", "复姓"),
        ("", "", "空字符串"),
        ("Zhang San", "Zhang San", "纯英文(按'英文保持不变'标注，原样保留)"),
        ("123", "123", "纯数字不变"),
    ]

    for text, expected, note in cases:
        actual = chinese_to_pinyin(text)
        if actual == expected:
            print(f"  [PASS] chinese_to_pinyin({text!r}) = {actual!r}  ({note})")
            passed += 1
        else:
            failed += 1
            failures.append((text, expected, actual, note))
            print(f"  [FAIL] chinese_to_pinyin({text!r}): expected={expected!r}, actual={actual!r}  ({note})")

    return passed, failed, failures


# ==================== 3. generate_sam_account_name 优先级测试 ====================
def test_generate_sam_priority():
    print("\n========== 3. generate_sam_account_name() 优先级测试 ==========")
    from ad_sync import generate_sam_account_name
    passed = 0
    failed = 0
    failures = []

    # (用户数据, 期望输出, 说明)
    cases = [
        (
            {"email": "zhangsan@realman.com", "account": "张三", "userid": "001", "name": "张三"},
            "zhangsan",
            "优先级1: 邮箱前缀优先",
        ),
        (
            {"email": "", "account": "lisi001", "userid": "002", "name": "李四"},
            "lisi001",
            "优先级2: 纯ASCII账号直接用",
        ),
        (
            {"email": "", "account": "张三001", "userid": "003", "name": "张三"},
            "zhangsan001",
            "优先级2: 含中文账号转拼音",
        ),
        (
            {"email": "", "account": "王五", "userid": "004", "name": "王五"},
            "wangwu",
            "优先级2: 纯中文账号转拼音",
        ),
        (
            {"email": "", "account": "", "userid": "005", "name": "赵六"},
            "zhaoliu",
            "优先级3: 空账号时姓名转拼音",
        ),
        (
            {"email": "", "account": "", "userid": "", "name": ""},
            "user0",
            "优先级4: 全空时hash兜底(hash('')==0)",
        ),
    ]

    for user_data, expected, note in cases:
        actual = generate_sam_account_name(user_data)
        if actual == expected:
            print(f"  [PASS] {note}: -> {actual!r}")
            passed += 1
        else:
            failed += 1
            failures.append((user_data, expected, actual, note))
            print(f"  [FAIL] {note}: expected={expected!r}, actual={actual!r}")
            print(f"         input={user_data}")

    return passed, failed, failures


# ==================== 4. sAMAccountName 长度限制测试 ====================
def test_length_limit():
    print("\n========== 4. sAMAccountName 长度限制(20字符)测试 ==========")
    from ad_sync import chinese_to_pinyin, generate_sam_account_name
    passed = 0
    failed = 0
    failures = []

    long_name = "欧阳龙飞凤舞先生测试员"  # 12个中文字
    full_pinyin = chinese_to_pinyin(long_name)
    print(f"  输入姓名: {long_name!r} (共{len(long_name)}个中文字)")
    print(f"  完整拼音: {full_pinyin!r} (共{len(full_pinyin)}字符)")

    # 验证完整拼音确实超过20字符
    if len(full_pinyin) > 20:
        print(f"  [PASS] 完整拼音长度{len(full_pinyin)} > 20，需要截断")
        passed += 1
    else:
        failed += 1
        failures.append(("pinyin_length", ">20", len(full_pinyin), "拼音长度不足20"))
        print(f"  [FAIL] 完整拼音长度{len(full_pinyin)} 未超过20")

    expected_truncated = full_pinyin[:20]
    actual = generate_sam_account_name(
        {"email": "", "account": "", "userid": "", "name": long_name}
    )

    if len(actual) <= 20:
        print(f"  [PASS] 生成结果长度{len(actual)} <= 20: {actual!r}")
        passed += 1
    else:
        failed += 1
        failures.append(("length", "<=20", len(actual), "未截断到20字符"))
        print(f"  [FAIL] 生成结果长度{len(actual)} > 20: {actual!r}")

    if actual == expected_truncated:
        print(f"  [PASS] 截断结果正确: {actual!r} (完整拼音前20字符)")
        passed += 1
    else:
        failed += 1
        failures.append(("truncated_value", expected_truncated, actual, "截断值不匹配"))
        print(f"  [FAIL] 截断结果不匹配: expected={expected_truncated!r}, actual={actual!r}")

    # 额外：直接通过 clean_sam_account_name 验证截断逻辑
    from ad_sync import clean_sam_account_name
    direct = clean_sam_account_name(full_pinyin)
    if len(direct) == 20 and direct == expected_truncated:
        print(f"  [PASS] clean_sam_account_name 截断正确: {direct!r}")
        passed += 1
    else:
        failed += 1
        failures.append(("clean_truncate", expected_truncated, direct, "clean截断异常"))
        print(f"  [FAIL] clean_sam_account_name 截断异常: expected={expected_truncated!r}, actual={direct!r}")

    return passed, failed, failures


# ==================== 5. 已有逻辑回归测试 ====================
def test_regression():
    print("\n========== 5. 已有逻辑回归测试 ==========")
    from ad_sync import clean_sam_account_name, escape_dn_value, get_domain_from_base_dn
    passed = 0
    failed = 0
    failures = []

    # clean_sam_account_name 回归
    clean_cases = [
        ("Zhang.San.", "zhang.san", "小写+去空格+去尾点"),
        ("Test User.Name", "testuser.name", "小写+去空格"),
        ("ABC 123!", "abc123", "小写+去空格+去非法字符"),
        ("a" * 25, "a" * 20, "25字符截断到20"),
        ("", "", "空字符串"),
        ("Hello_World-Test", "hello_world-test", "下划线短横线保留"),
    ]
    for raw, expected, note in clean_cases:
        actual = clean_sam_account_name(raw)
        if actual == expected:
            print(f"  [PASS] clean_sam_account_name({raw[:15]!r}...) = {actual!r}  ({note})")
            passed += 1
        else:
            failed += 1
            failures.append((raw, expected, actual, note))
            print(f"  [FAIL] clean_sam_account_name({raw!r}): expected={expected!r}, actual={actual!r}  ({note})")

    # escape_dn_value 回归
    escape_cases = [
        ("test,name", "test\\,name", "逗号转义"),
        ("CN=Test", "CN\\=Test", "等号转义"),
        ("a\\b", "a\\\\b", "反斜杠转义"),
        ("plain", "plain", "无特殊字符"),
        ("a+b<c>d;e\"f", "a\\+b\\<c\\>d\\;e\\\"f", "多特殊字符转义"),
    ]
    for raw, expected, note in escape_cases:
        actual = escape_dn_value(raw)
        if actual == expected:
            print(f"  [PASS] escape_dn_value({raw!r}) = {actual!r}  ({note})")
            passed += 1
        else:
            failed += 1
            failures.append((raw, expected, actual, note))
            print(f"  [FAIL] escape_dn_value({raw!r}): expected={expected!r}, actual={actual!r}  ({note})")

    # get_domain_from_base_dn 回归
    domain_cases = [
        ("OU=Users,OU=REALMAN,DC=corp,DC=realman-robot,DC=com", "corp.realman-robot.com", "多级域名"),
        ("DC=example,DC=com", "example.com", "简单域名"),
    ]
    for base_dn, expected, note in domain_cases:
        actual = get_domain_from_base_dn(base_dn)
        if actual == expected:
            print(f"  [PASS] get_domain_from_base_dn -> {actual!r}  ({note})")
            passed += 1
        else:
            failed += 1
            failures.append((base_dn, expected, actual, note))
            print(f"  [FAIL] get_domain_from_base_dn: expected={expected!r}, actual={actual!r}  ({note})")

    return passed, failed, failures


# ==================== 6. 端到端验证：英文姓名经完整管线归一化 ====================
def test_end_to_end_english():
    """验证含英文空格/大写的姓名经 generate_sam_account_name 后正确归一化为小写无空格"""
    print("\n========== 6. 端到端验证：英文姓名归一化 ==========")
    from ad_sync import generate_sam_account_name
    passed = 0
    failed = 0
    failures = []

    # 姓名 "Zhang San" 经 优先级3(姓名转拼音) -> clean_sam_account_name -> "zhangsan"
    actual = generate_sam_account_name(
        {"email": "", "account": "", "userid": "x", "name": "Zhang San"}
    )
    expected = "zhangsan"
    if actual == expected:
        print(f"  [PASS] name='Zhang San' 端到端归一化 -> {actual!r}")
        passed += 1
    else:
        failed += 1
        failures.append(("Zhang San e2e", expected, actual, "英文端到端归一化失败"))
        print(f"  [FAIL] name='Zhang San' 端到端: expected={expected!r}, actual={actual!r}")

    return passed, failed, failures


# ==================== 主函数 ====================
def main():
    print("=" * 70)
    print("钉钉AD同步工具 - 拼音转换功能测试报告")
    print("=" * 70)

    total_passed = 0
    total_failed = 0
    all_failures = []

    # 1. 依赖测试
    p, f = test_dependency()
    total_passed += p
    total_failed += f

    # 2. chinese_to_pinyin 测试
    p, f, fails = test_chinese_to_pinyin()
    total_passed += p
    total_failed += f
    all_failures.extend([("chinese_to_pinyin", *x) for x in fails])

    # 3. 优先级测试
    p, f, fails = test_generate_sam_priority()
    total_passed += p
    total_failed += f
    all_failures.extend([("generate_priority", *x) for x in fails])

    # 4. 长度限制测试
    p, f, fails = test_length_limit()
    total_passed += p
    total_failed += f
    all_failures.extend([("length_limit", *x) for x in fails])

    # 5. 回归测试
    p, f, fails = test_regression()
    total_passed += p
    total_failed += f
    all_failures.extend([("regression", *x) for x in fails])

    # 6. 端到端英文归一化
    p, f, fails = test_end_to_end_english()
    total_passed += p
    total_failed += f
    all_failures.extend([("e2e_english", *x) for x in fails])

    # 汇总
    print("\n" + "=" * 70)
    print("测试汇总")
    print("=" * 70)
    print(f"  总测试数: {total_passed + total_failed}")
    print(f"  通过:     {total_passed}")
    print(f"  失败:     {total_failed}")

    if all_failures:
        print("\n失败详情:")
        for module, case, expected, actual, note in all_failures:
            print(f"  [{module}] {case!r}: expected={expected!r}, actual={actual!r} ({note})")

    print("\n" + "=" * 70)
    if total_failed == 0:
        print("结论: 全部测试通过 [OK]")
        return 0
    else:
        print(f"结论: {total_failed} 项测试失败 [FAIL]")
        return 1


if __name__ == "__main__":
    sys.exit(main())
