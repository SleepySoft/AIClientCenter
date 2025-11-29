PROMPT = """
# 角色设定
你是一个
# 核心规则
专业情报分析师，需对输入文本进行结构化解析与情报价值评估。

1.  **输出语言强制规定**
    无论输入文本使用何种语言，输出必须全部使用**中文（简体）**，不得保留日文或繁体中文等类似但并非简体中文的信息。对文本中出现的外国人名、地名、机构名，需采用广泛认可的中文媒体通用译名。

2.  **情报价值一级过滤（最高优先级）**
    首先对输入文本的**整体内容和目的**进行判断。如果文本主题属于以下**无情报价值**的类别，则**立即终止**处理，仅输出：`{"UUID": "输入的UUID原值"}`。

    **无情报价值类型清单：**
    *   **文艺创作类：** 文学、影视剧情、音乐赏析、艺术评论、娱乐八卦。
    *   **营销推广类：** 产品广告、商业宣传、营销软文、购物推荐。
    *   **生活服务类：** 旅游攻略、餐厅点评、使用指南、个人建议。
    *   **主观表达类：** 个人博客、日记、情感抒发、非时政类社会评论。
    *   **历史与学术类：** 纯粹的历史事件回顾、无立即应用价值的学术论文。
    *   **体育竞技类：** 所有体育赛事报道、运动员新闻等。
    *   **日常社交类：** 祝福语、问候信、请柬、无影响的公告。

3.  **含情报价值文本的处理流程**
    只有完全排除规则2的情况后，才可判定文本具有情报价值，并继续执行结构化分析。输出必须是一个**严格的、完整的JSON对象**，不得包含任何JSON之外的文本。

    **具有情报价值的类型指引：**
    *   地缘政治与军事动态
    *   国际关系与外交事件
    *   国家重大政策与法规
    *   政治动态与安全事件
    *   具有系统性影响的经济与金融事件
    *   具有战略意义的科技与工业突破
    *   任何对国际局势、国家安全、经济运行或社会稳定产生实质性影响的信息。

# 处理流程
1.  **要素提取：** 按字段顺序结构化提取五要素（TIME，LOCATION，PEOPLE，ORGANIZATION，EVENT_BRIEF）。
2.  **标题生成：** 生成信息高度浓缩的事件标题。
3.  **分类评估：** 根据提供的评分标准对情报进行分类和价值分析。通常一条情报的核心价值属于一个主要分类。

# 输出要求 - 有效JSON对象
{
  "UUID": "输入的UUID原值，通常在metadata中，无则为null",
  "INFORMANT": "信息来源描述，通常在metadata中。如果输入的元数据（如上下文）提供原始文章的直接URL，则放入此URL。否则，尝试从正文识别并精炼提取明确提及的权威发布机构名称（如'路透社'、'中国外交部'），若无则为空字符串。",
  "PUB_TIME": "信息发布的时间，通常在metadata中。YYYY-MM-DD格式，无则null"

  "TIME": ["信息中涉及到的时间，YYYY-MM-DD格式，无则空列表[]", ...],
  "LOCATION": ["列表形式存放文章主体中涉及的国家/省/市/具体地址等精炼的地名描述词。可包含不同层级的地名。无则空列表[]。", ... ],
  "PEOPLE": ["文章主体中涉及的、有明确指代的姓名列表。无则空列表[]。", ... ],
  "ORGANIZATION": ["文章主体中涉及的国家、公司、宗教、机构、组织名称列表。无则空列表[]。", ... ],
  "EVENT_TITLE": "20字内高度凝练、描述核心情报内容的标题。",
  "EVENT_BRIEF": "50字内精要描述事件核心事实的摘要。",
  "EVENT_TEXT": "去除广告及无关信息后，对核心事件内容进行的简洁、准确的提炼与重写。如原文为外文，则需进行完全本地化的流畅翻译，杜绝翻译腔。无字数限制。",
  "RATE": { // *评分均为整数，每个维度的评分细则见下方“评分标准”对应的条目*
    "国家政策": 0-10,
    "国际关系": 0-10,
    "政治影响": 0-10,
    "商业金融": 0-10,
    "科技信息": 0-10,
    "社会事件": 0-10,
    "其它信息": 0-10, // 注：若评估为6分或以上，需说明独特性。

    "内容准确率": 0-10, // 注：*独立评分*
    "规模及影响": 0-10, // 注：*独立评分*
    "潜力及传承": 0-10  // 注：*独立评分*
  },
  "IMPACT": "简述选择该情报主要价值分类的原因（为什么该项评分最高）及评分理由（依据评分标准说明）。字数严格控制在50字以内。",
  "TIPS": "给用户的非必选备注信息（如处理难点说明、关键提醒）。字数严格控制在50字以内。若无重要备注可置空。"
}

# 评分标准

## 总体评分原则
*   **从严评估：** 6分是“值得高级别决策者关注”的重要门槛，8分代表“具有区域或行业颠覆性影响”，10分应为“历史性/全球性里程碑事件”。绝大多数情报应集中在4分及以下。
*   **高分稀缺性：** 除非事件确实达到相应影响力，否则不应轻易给予6分及以上评分。
*   **实质性影响：** 评分应基于事件可能带来的实际后果，而非仅仅基于话题热度或媒体报道量。

## 国家政策
    10分：国家级战略转向或极度罕见的重大决策（如战争动员、全国紧急状态）。
    8分：影响国家核心竞争力或民生根本的重大政策（如税制重大改革、金融系统核心政策调整）。
    6分：对特定行业或较大人口群体产生实质性、可感知影响的政策。
    4分：常规政策调整，影响范围有限或影响较为间接。
    2分：地方性政策或执行细则，不具备全国性影响。
    0分：低关联或无关联

## 国际关系
    10分：彻底改变国际格局或引发大国直接对抗的风险（如重大战争爆发、主要军事同盟变更）。
    8分：显著改变地区力量平衡或导致主要国家间关系根本性变化（如重大制裁、断交、重要军事协议）。
    6分：具有实质性合作成果或紧张局势缓和的国事访问与高级别会谈。
    4分：例行性外交活动或涉及非主要国家的国际事件。
    2分：象征性外交活动或日常国际交流。
    0分：低关联或无关联

## 政治影响
    10分：导致国家最高领导层非正常更迭或政权稳定性受到严峻挑战的事件。
    8分：影响中央决策层构成或国家重大政治议程的事件（如重要高层官员变动、核心政治会议的重大决议）。
    6分：重要省部级官员变动或引发全国性政治关注的事件。
    4分：地方性政治事件或常规人事任免。
    2分：日常政治活动或影响甚微的政治新闻。
    0分：低关联或无关联

## 商业金融
    10分：可能引发系统性金融风险或国际金融市场剧烈震荡的事件（如主要中央银行极端政策、超大型金融机构倒闭）。
    8分：对全国性金融市场或重要行业产生重大影响的事件（如主要交易所重大规则变化、大型垄断企业危机）。
    6分：对区域经济或特定行业产生实质性影响的企业事件（如中型金融机构问题、重要上市公司重大变故）。
    4分：公司层面的重要动向，但影响范围有限。
    2分：日常商业资讯或小型企业事件。
    0分：低关联或无关联

## 科技信息
    10分：颠覆现有技术范式并可能引发产业革命的突破性技术。
    8分：解决重大技术难题或创造全新市场领域的核心技术突破。
    6分：重要技术迭代或研发进展，对行业格局产生明显影响。
    4分：技术进步或研发动向，但短期内难以产生实质性影响。
    2分：常规技术报道或产品发布。
    0分：低关联或无关联

## 社会事件
    10分：造成重大人员伤亡或引发全国性社会动荡的极端事件。
    8分：产生广泛社会影响或引发政策反思的恶性事件/重大冲突。
    6分：具有典型性或在特定范围内引起广泛关注的社会事件（国内事件标准从严）。
    4分：地方性社会事件或具有一定新闻价值但影响有限的事件（国外事件）。
    2分：个别案例或社区级别的事件。
    0分：低关联或无关联

## 其它信息
    8分：极具情报价值的新兴领域突破或特殊渠道获取的稀缺信息。
    6分：具有一定独特性或预警价值的信息。
    4分：常规信息，缺乏明显情报价值。
    2分：低价值信息。
    0分：无情报价值

## 内容准确率
    10分：多方权威信源交叉验证，信息高度可靠。
    8分：单一权威信源，信息逻辑完整。
    6分：可信媒体报道，但缺乏原始数据支撑。
    4分：信息基本可信，但部分细节存疑。
    2分：信息来源模糊，可靠性存疑。
    0分：来源不可靠或信息明显失实

## 规模及影响
    10分：全球性影响，改变国际秩序。
    8分：影响世界主要区域或多个大国。
    6分：影响一个国家或多个重要领域。
    4分：国内区域性影响或行业性影响。
    2分：局部地区影响。
    0分：影响甚微

## 潜力及传承
    10分：改变国际格局的历史性事件。
    8分：可能成为国家发展转折点的重大事件。
    6分：引发长期关注和后续发展的重大事件。
    4分：会有后续报道但影响有限的事件。
    2分：短期关注的事件。
    0分：一次性事件，无后续影响

# 附加指引
*   **评分逻辑：** 评估`RATE`时，首要任务是判断该情报的**主要价值维度**属于哪个分类（通常是评分最高的那个）。其它非主要分类的评分应严格依据其自身定义给出，通常较低（0-4分）。`内容准确率`单独评分不受此限制。
*   **格式要求：** 最终输出必须是一个可以被标准JSON解析器正确解析的有效JSON字符串。确保字段名称拼写准确，数据类型正确。
"""


USER_MESSAGE = """
## metadata
- UUID: eea9b861-5b50-41b4-a387-b76b08d5beeb
- title: Ecuadorean President Daniel Noboa unharmed after attack on his car
- authors: []
- pub_time: [2025, 10, 7, 22, 56, 20, 1, 280, 0]
- informant: https://www.aljazeera.com/news/2025/10/7/ecuadorean-president-daniel-noboa-unharmed-after-attack-on-his-car?traffic_source=rss

## 正文内容
[News](/news/)|[Politics](/tag/politics/)

# Ecuadorean President Daniel Noboa unharmed after attack on his car

 _A government minister claimed the attack was an assassination attempt, but
protesters said they were the ones targeted with violence._

Ecuador's President Daniel Noboa was re-elected to his first full term in
April [Adriano Machado/Reuters]

By [Abby Rogers](/author/rogersa) and Reuters

Published On 7 Oct 20257 Oct 2025

Click here to share on social media

share2

Share

[facebook](https://www.facebook.com/sharer/sharer.php?u=https%3A%2F%2Faje.io%2F28p5ev)[twitter](https://twitter.com/intent/tweet?text=Ecuadorean%20President%20Daniel%20Noboa%20unharmed%20after%20attack%20on%20his%20car&source=sharethiscom&related=sharethis&via=AJEnglish&url=https%3A%2F%2Faje.io%2F28p5ev)[whatsapp](whatsapp://send?text=https%3A%2F%2Faje.io%2F28p5ev)[copylink](https://aje.io/28p5ev)

Save

A government official in Ecuador has accused protesters of attempting to
attack President Daniel Noboa, alleging that a group of approximately 500
people surrounded his vehicle and threw rocks.

The attack, which unfolded in the south-central province of Canar, took place
as Noboa arrived in the canton of El Tambo for an event about water treatment
and sewage.

## Recommended Stories

list of 3 items

  * list 1 of 3[Ecuador’s Noboa wins presidential run-off, rival demands recount](/news/2025/4/14/ecuadors-noboa-wins-presidential-runoff-rival-demands-recount)
  * list 2 of 3[Ecuador accuses ‘bad losers’ of assassination plot against President Noboa](/news/2025/4/19/ecuador-accuses-bad-losers-of-assassination-plot-against-president-noboa)
  * list 3 of 3[Ecuador’s Daniel Noboa sworn in for full term, promising a crackdown on gangs](/news/2025/5/24/ecuadors-daniel-noboa-sworn-in-for-full-term-promising-a-crackdown-on-gangs)

end of list

Environment and Energy Minister Ines Manzano said Noboa’s car showed “signs of
bullet damage”. In a statement to the press, she explained that she filed a
report alleging an assassination attempt had taken place.

“Shooting at the president’s car, throwing stones, damaging state property —
that’s just criminal,” Manzano said. “We will not allow this.”

The president’s office also issued a
[statement](https://x.com/Presidencia_Ec/status/1975659377240523162) after the
attack on Tuesday, pledging to pursue accountability against those involved.

“Obeying orders to radicalise, they attacked a presidential motorcade carrying
civilians. They attempted to forcibly prevent the delivery of a project
intended to improve the lives of a community,” the statement, published on
social media, said.

“All those arrested will be prosecuted for terrorism and attempted murder,” it
added.

Five people, according to Manzano, have been detained following the incident.
Noboa was not injured.

Video published by the president’s office online shows Noboa’s motorcade
navigating a roadway lined with protesters, some of whom picked up rocks and
threw them at the vehicles, causing fractures to form on the glass.

A separate image showed a silver SUV with a shattered passenger window and a
shattered windscreen. It is not clear from the images whether a bullet had
been fired.

Noboa, [Ecuador’s](/news/2025/4/19/ecuador-accuses-bad-losers-of-
assassination-plot-against-president-noboa) youngest-ever president, was re-
elected in April after a heated run-off election against left-wing rival Luisa
Gonzalez.

Advertisement

May marked the start of his [first full term](/news/2025/5/24/ecuadors-daniel-
noboa-sworn-in-for-full-term-promising-a-crackdown-on-gangs) in office.
Previously, Noboa, a conservative candidate who had only served a single term
in the National Assembly, had been elected to serve the remainder of Guillermo
Lasso’s term — a period of around 18 months — after the former president
dissolved his government.

Combatting crime has been a centrepiece of Noboa’s pitch for the presidency.
Ecuador, formerly considered an “island of peace” in South America, has seen a
spike in homicide rates as criminal organisations seek to expand their drug
trafficking routes through the country.

Ecuador’s economy has also struggled to recover following the COVID-19
pandemic.

But Noboa has faced multiple protests since taking office.

In recent weeks, for example, he has faced outcry over his decision to end a
fuel subsidy that critics say helps lower-income families.

Noboa’s government, however, has argued that the subsidy drove up government
costs without reaching those who need it. In a presidential
[statement](https://x.com/ComunicacionEc/status/1966652778157052275/photo/1)
on September 12, officials accused the subsidy of being “diverted to
smuggling, illegal mining and undue benefits”.

The statement also said that the subsidies represented $1.1bn that could
instead be used to compensate small farmers and transportation workers
directly.

But the Confederation of Indigenous Nationalities of Ecuador (CONAIE), the
country’s most powerful Indigenous advocacy organisation, launched a strike in
response to the news of the subsidy’s end.

It called upon its supporters to lead protests and block roadways as a way of
expressing their outrage.

Nevertheless, on Tuesday, the group denied that there had been an organised
attack on Noboa’s motorcade. Instead, CONAIE argued that government violence
had been “orchestrated” against the people who had gathered to protest Noboa.

“We denounce that at least five comrades have been arbitrarily detained,”
[CONAIE posted on X](https://x.com/CONAIE_Ecuador/status/1975651759084159167).
“Among the attacked are elderly women.”

It noted that Tuesday marked the 16th day of protest. “The people are not the
enemy,” it [added](https://x.com/CONAIE_Ecuador/status/1975673584979706334).

CONAIE had largely [backed](/news/longform/2025/4/10/ecuadors-indigenous-
movement-splinters-over-presidential-election-support) Noboa’s rival Gonzalez
in the April election, though some of its affiliate groups splintered in
favour of Noboa.

This is not the first time that Noboa’s government has claimed the president
was the target of an assassination attempt.

In April, shortly after the run-off vote, it issued a [“maximum
alert”](/news/2025/4/19/ecuador-accuses-bad-losers-of-assassination-plot-
against-president-noboa) claiming that assassins had entered the country from
Mexico to destabilise his administration.

At the time, the administration blamed “sore losers” from the election for
fomenting the alleged plot.

Advertisement
"""


MESSAGE = [
    {
        "role": "system",
        "content": PROMPT
    },
    {
        "role": "user",
        "content": USER_MESSAGE
    }
]
