"""Short Chinese descriptions for the exchange and THS directories."""
import re
from pathlib import Path


def load_glossary():
    glossary = {}
    path = Path(__file__).with_name("sector_glossary.tsv")
    for line in path.read_text(encoding="utf-8").splitlines():
        terms, meaning = line.split("\t")
        for term in terms.split("|"):
            if term in glossary:
                raise ValueError(f"Duplicate glossary entry: {term}")
            glossary[term] = meaning
    return glossary


GLOSSARY = load_glossary()
MARKET_SCOPE = (
    ("上证科创板", "科创板"), ("上证380", "上证380"), ("上证180", "上证180"),
    ("沪深300", "沪深300"), ("中证500", "中证500"), ("中证800", "中证800"),
    ("国证1000", "国证1000"), ("中证全指", "中证全指"), ("创业板", "创业板"),
    ("上证", "沪市"), ("深证", "深市"), ("深成", "深证成指"), ("深A", "深市A股"),
    ("国证", "国证"), ("中证", "中证"),
)


def exchange_intro(row):
    specific = {
        "000685.SH": "观察科创板芯片设计、制造及相关环节的企业表现",
        "980017.SZ": "反映国证半导体芯片产业相关上市公司的表现",
        "399284.SZ": "展示深市人工智能技术与应用主题的样本走势",
        "970070.SZ": "关注创业板人工智能技术及应用相关企业",
    }
    if row["code"] in specific:
        return specific[row["code"]]
    title = row["fullname"].removesuffix("指数")
    prefix, scope = next(((a, b) for a, b in MARKET_SCOPE if title.startswith(a)), ("", ""))
    subject = title.removeprefix(prefix)
    subject = re.sub(r"(?:产业主题|行业投资|行业|产业|主题)$", "", subject)
    term = re.sub(r"(?:50|100|60|80)$", "", subject)
    # Keep distinctions such as 央视财经50 / 央视财经500 and then try unbranded industry names.
    unbranded = re.sub(r"^(?:细分|申万|方正富邦)", "", term)
    meaning = (GLOSSARY.get(title.removeprefix(prefix)) or GLOSSARY.get(subject)
               or GLOSSARY.get(term) or GLOSSARY.get(unbranded))
    if meaning:
        text = f"{scope}样本，{meaning}" if scope else meaning
        return text if len(text) <= 30 else meaning
    if scope and subject:
        return f"观察{scope}样本中{subject}相关股票的走势"
    return f"观察{title}所涵盖股票的整体表现"


def ths_intro(name, kind):
    clean = name.removesuffix("(A股)").removesuffix("指数")
    if kind == "I":
        clean = re.sub(r"(?:[ⅠⅡⅢⅣⅤ]+|I{1,3}|IV)$", "", clean)
    short = clean.removesuffix("概念")
    meaning = GLOSSARY.get(clean) or GLOSSARY.get(short)
    if kind == "I" and not meaning:
        # National industry classifications use both punctuated and plain titles.
        meaning = GLOSSARY.get(re.sub(r"[、，, ]", "", short))
    if meaning:
        return meaning
    if kind == "TH":
        if clean.startswith("同花顺金仓"):
            return ("含现金分红再投资的同花顺金仓系列收益表现"
                    if clean.endswith("全收益") else "观察同花顺金仓系列对应样本的价格走势")
        if clean in {"东北", "华北", "华东", "华南", "华中", "西北", "西南"}:
            return f"按{clean}地区范围观察上市公司的市场表现"
        if clean.startswith("地方国企"):
            area = clean.removeprefix("地方国企")
            return f"观察{area}地方国有企业的市场表现" if area else "观察地方国有企业这一所有制群体的市场表现"
        if clean.endswith(("市", "自治区", "自治", "城市群", "都市圈")):
            return f"按地理范围归集与{clean}相关的上市公司"
        return f"观察{clean}主题所涵盖上市公司的市场表现"
    if kind == "I":
        if short.endswith(("制造", "制造业", "生产", "加工", "开采")):
            return f"观察从事{short.removesuffix('业')}业务的相关企业"
        if short.endswith(("设备", "器械", "工具", "机械", "仪器", "元件", "零部件")):
            return f"关注{short}产品及其配套供应企业"
        if short.endswith(("服务", "零售", "运输", "贸易", "开发", "工程", "保险")):
            return f"观察提供{short}业务的上市公司表现"
        if short.endswith(("材料", "制品", "化学品", "用品", "产品", "元器件")):
            return f"关注{short}的生产、加工与供应"
        if short.startswith("其他"):
            return f"观察归入{short}细分行业的公司表现"
        return f"观察{short}行业相关上市公司的表现"
    if short.endswith(("自贸区", "新区", "自贸港")):
        return f"关注{short}建设与区内经贸活动相关企业"
    if short.startswith("参股"):
        return f"关注以{short}为共同特征的上市公司"
    if short.endswith(("成份股", "样本股")):
        return f"按{short}范围观察入选股票表现"
    if short.endswith("预增"):
        return f"关注披露{short}业绩预告的上市公司"
    return f"关注{short}题材涉及的业务和相关公司"


def checked_intro(text):
    if not (10 <= len(text) <= 30 and len(re.findall(r"[\u3400-\u9fff]", text)) >= 10):
        raise ValueError(f"Review description length: {text!r}")
    return text
