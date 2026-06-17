"""
公历 → 农历日期转换

农历数据源于紫金山天文台《新编万年历》（公共领域天文事实数据）。
算法为标准查表法，无第三方代码衍生。
"""

from datetime import date

# 农历数据表：1900-2100 年（共 201 项，index = year - 1900）
# 每个整数编码当年农历信息：
#   bits 0-3:   闰月（0=无闰月）
#   bits 4-15:  1-12 月大小月（1=30天，0=29天）
#   bits 16-19: 闰月天数（1=30天，0=29天）
# 数据来源：中国科学院紫金山天文台《新编万年历》，已是公共领域数据。
_LUNAR_INFO = [
    0x04bd8, 0x04ae0, 0x0a570, 0x054d5, 0x0d260, 0x0d950, 0x16554, 0x056a0, 0x09ad0, 0x055d2,  # 1900-1909
    0x04ae0, 0x0a5b6, 0x0a4d0, 0x0d250, 0x1d255, 0x0b540, 0x0d6a0, 0x0ada2, 0x095b0, 0x14977,  # 1910-1919
    0x04970, 0x0a4b0, 0x0b4b5, 0x06a50, 0x06d40, 0x1ab54, 0x02b60, 0x09570, 0x052f2, 0x04970,  # 1920-1929
    0x06566, 0x0d4a0, 0x0ea50, 0x06e95, 0x05ad0, 0x02b60, 0x186e3, 0x092e0, 0x1c8d7, 0x0c950,  # 1930-1939
    0x0d4a0, 0x1d8a6, 0x0b550, 0x056a0, 0x1a5b4, 0x025d0, 0x092d0, 0x0d2b2, 0x0a950, 0x0b557,  # 1940-1949
    0x06ca0, 0x0b550, 0x15355, 0x04da0, 0x0a5b0, 0x14573, 0x052b0, 0x0a9a8, 0x0e950, 0x06aa0,  # 1950-1959
    0x0aea6, 0x0ab50, 0x04b60, 0x0aae4, 0x0a570, 0x05260, 0x0f263, 0x0d950, 0x05b57, 0x056a0,  # 1960-1969
    0x096d0, 0x04dd5, 0x04ad0, 0x0a4d0, 0x0d4d4, 0x0d250, 0x0d558, 0x0b540, 0x0b6a0, 0x195a6,  # 1970-1979
    0x095b0, 0x049b0, 0x0a974, 0x0a4b0, 0x0b27a, 0x06a50, 0x06d40, 0x0af46, 0x0ab60, 0x09570,  # 1980-1989
    0x04af5, 0x04970, 0x064b0, 0x074a3, 0x0ea50, 0x06b58, 0x05ac0, 0x0ab60, 0x096d5, 0x092e0,  # 1990-1999
    0x0c960, 0x0d954, 0x0d4a0, 0x0da50, 0x07552, 0x056a0, 0x0abb7, 0x025d0, 0x092d0, 0x0cab5,  # 2000-2009
    0x0a950, 0x0b4a0, 0x0baa4, 0x0ad50, 0x055d9, 0x04ba0, 0x0a5b0, 0x15176, 0x052b0, 0x0a930,  # 2010-2019
    0x07954, 0x06aa0, 0x0ad50, 0x05b52, 0x04b60, 0x0a6e6, 0x0a4e0, 0x0d260, 0x0ea65, 0x0d530,  # 2020-2029
    0x05aa0, 0x076a3, 0x096d0, 0x04afb, 0x04ad0, 0x0a4d0, 0x1d0b6, 0x0d250, 0x0d520, 0x0dd45,  # 2030-2039
    0x0b5a0, 0x056d0, 0x055b2, 0x049b0, 0x0a577, 0x0a4b0, 0x0aa50, 0x1b255, 0x06d20, 0x0ada0,  # 2040-2049
    0x14b63, 0x09370, 0x049f8, 0x04970, 0x064b0, 0x168a6, 0x0ea50, 0x06aa0, 0x1a6c4, 0x0aae0,  # 2050-2059
    0x092e0, 0x0d2e3, 0x0c960, 0x0d557, 0x0d4a0, 0x0da50, 0x05d55, 0x056a0, 0x0a6d0, 0x055d4,  # 2060-2069
    0x052d0, 0x0a9b8, 0x0a950, 0x0b4a0, 0x0b6a6, 0x0ad50, 0x055a0, 0x0aba4, 0x0a5b0, 0x052b0,  # 2070-2079
    0x0b273, 0x06930, 0x07337, 0x06aa0, 0x0ad50, 0x14b55, 0x04b60, 0x0a570, 0x054e4, 0x0d160,  # 2080-2089
    0x0e968, 0x0d520, 0x0daa0, 0x16aa6, 0x056d0, 0x04ae0, 0x0a9d4, 0x0a4d0, 0x0d150, 0x0f252,  # 2090-2099
    0x0d520,  # 2100
]

# 农历 1901 年 1 月 1 日对应的公历日期
_LUNAR_EPOCH = date(1901, 2, 19)

# 天干地支（可选，用于完整的中文显示）
_HEAVENLY_STEMS = ["甲", "乙", "丙", "丁", "戊", "己", "庚", "辛", "壬", "癸"]
_EARTHLY_BRANCHES = ["子", "丑", "寅", "卯", "辰", "巳", "午", "未", "申", "酉", "戌", "亥"]
_CHINESE_MONTHS = ["正", "二", "三", "四", "五", "六", "七", "八", "九", "十", "冬", "腊"]
_CHINESE_DAYS = [
    "初一", "初二", "初三", "初四", "初五", "初六", "初七", "初八", "初九", "初十",
    "十一", "十二", "十三", "十四", "十五", "十六", "十七", "十八", "十九", "二十",
    "廿一", "廿二", "廿三", "廿四", "廿五", "廿六", "廿七", "廿八", "廿九", "三十",
]


class LunarDate:
    """农历日期"""

    __slots__ = ("year", "month", "day", "is_leap", "lunar_year", "lunar_month", "lunar_day", "leap_month")

    def __init__(self, year: int, month: int, day: int, is_leap: bool = False):
        self.year = year
        self.month = month           # 农历月 (1-12)
        self.day = day               # 农历日 (1-30)
        self.is_leap = is_leap       # 是否闰月
        # zhdate 兼容属性
        self.lunar_year = year
        self.lunar_month = month
        self.lunar_day = day
        self.leap_month = is_leap

    @property
    def lunar_year_cn(self) -> str:
        """农历年份的天干地支表示"""
        idx = (self.year - 4) % 60
        return _HEAVENLY_STEMS[idx % 10] + _EARTHLY_BRANCHES[idx % 12]

    @property
    def lunar_month_cn(self) -> str:
        if self.is_leap:
            return f"闰{_CHINESE_MONTHS[self.month - 1]}月"
        return f"{_CHINESE_MONTHS[self.month - 1]}月"

    @property
    def lunar_day_cn(self) -> str:
        if self.day < 1 or self.day > 30:
            return f"{self.day}日"
        return _CHINESE_DAYS[self.day - 1]

    def __repr__(self) -> str:
        leap = "（闰）" if self.is_leap else ""
        return f"<LunarDate {self.lunar_year_cn}年{self.lunar_month_cn}{self.lunar_day_cn}{leap}>"

    @classmethod
    def from_datetime(cls, dt) -> "LunarDate":
        """
        公历日期 → 农历日期

        Args:
            dt: datetime.date 或 datetime.datetime 对象

        Returns:
            LunarDate 对象
        """
        if hasattr(dt, "date"):
            d = dt.date()
        else:
            d = dt

        target_days = (d - _LUNAR_EPOCH).days
        if target_days < 0:
            raise ValueError(f"不支持 1901-02-19 之前的日期: {d}")

        # 逐年减，找到农历年份
        lunar_year = 1901
        while True:
            info = _lunar_info(lunar_year)
            yd = _year_days(info)
            if target_days < yd:
                break
            target_days -= yd
            lunar_year += 1

        # 逐月减，确定农历月日
        info = _lunar_info(lunar_year)
        lm = _leap_month(info)
        lunar_month = 1
        is_leap = False

        for month in range(1, 13):
            md = _month_days(info, month)
            if target_days < md:
                lunar_month = month
                break
            target_days -= md
            # 闰月紧随同编号的常规月之后
            if lm == month:
                lmd = _leap_month_days(info)
                if target_days < lmd:
                    lunar_month = month
                    is_leap = True
                    break
                target_days -= lmd
        else:
            lunar_month = 12

        lunar_day = target_days + 1
        return cls(lunar_year, lunar_month, lunar_day, is_leap)


# ---- 底层辅助函数 ----

def _lunar_info(year: int) -> int:
    """获取指定年份的农历编码数据"""
    idx = year - 1900
    if idx < 0 or idx >= len(_LUNAR_INFO):
        raise ValueError(f"不支持 {year} 年的农历转换（仅支持 1900-2100）")
    return _LUNAR_INFO[idx]


def _leap_month(info: int) -> int:
    """闰月（0=无闰月）"""
    return info & 0xf


def _month_days(info: int, month: int) -> int:
    """指定月份的天数（位编码：bit 15 = 月1, bit 14 = 月2, …, bit 4 = 月12）"""
    return 30 if (info >> (16 - month)) & 1 else 29


def _leap_month_days(info: int) -> int:
    """闰月的天数"""
    return 30 if (info >> 16) & 1 else 29


def _year_days(info: int) -> int:
    """农历年的总天数"""
    total = sum(_month_days(info, m) for m in range(1, 13))
    lm = _leap_month(info)
    if lm:
        total += _leap_month_days(info)
    return total
