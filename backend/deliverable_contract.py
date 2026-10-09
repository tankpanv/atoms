"""User deliverables are independent of the code used to produce them."""
from pathlib import Path

FORMATS = {'pptx', 'ppt', 'pdf', 'docx', 'doc', 'xlsx', 'xls', 'csv', 'md', 'txt',
           'html', 'png', 'jpg', 'jpeg', 'webp', 'svg', 'zip'}
INTENTS = {'software', 'presentation', 'report', 'document', 'spreadsheet', 'analysis', 'mixed', 'other'}
INSTRUCTIONS = '''
首先判断真正的用户目标与最终成果，不把所有请求转成软件开发：用户要PPT就交付可下载、可逐页预览的真实PPT；报告/文档交付正文和文件；数据分析交付结论、方法、数据来源和需要的表格/图表。生成这些成果的Python脚本只是生产工具，不是用户成果。用户要网站/应用才建立服务和默认Web模板。
计划新增 task_type（software|presentation|report|document|spreadsheet|analysis|mixed|other）和 deliverables 列表。纯文件任务 application_type=artifact，无需前后端架构、dev、软件README或单元测试；commands.build 写真实生成命令，commands.test 可为空，架构可省略。混合需求保留适合运行软件的 application_type 并同时列出文件成果。
每个文件成果严格格式：{"path":"dist/报告.pptx","title":"用户看到的成果名称","format":"pptx","requirement_ids":["R1"],"min_units":1,"content_checks":["必须在成果正文中出现的标题或关键词"]}。format为pptx|ppt|pdf|docx|doc|xlsx|xls|csv|md|txt|html|png|jpg|jpeg|webp|svg|zip，扩展名必须相同；路径仅限dist内非隐藏文件，无..；min_units为1–10000整数（PPT/PDF/Word页数、Excel非空行（含表头）、CSV文件行（含表头）、其他为1）；content_checks为0–40个1–200字符的字面正文片段，文本成果至少一个，按真实需求设定，不检查脚本源码。requirement_ids覆盖该成果相关需求；文件验收verification=artifact。
分析/报告必须说明受众、问题、数据来源/时效口径、方法、结论和限制。需要最新事实时通过run_shell实际获取权威资料并在成果中标注来源/日期，不能凭空编造引用或把示例数据冒充真实数据；无法取得的数据如实标注。遵循用户指定格式/模板，不擅自用网页替代PPT或文档。未指定格式时按阅读、演示、分析目标选择合适成果，必要时提供互补格式。
平台已安装python-pptx、python-docx、openpyxl、pypdf；可直接生成Office文件，不必重新安装同一依赖。平台用LibreOffice渲染Office页面，用表格阅读器展示Excel/CSV。PPT需有真实文字/图表/布局；Word/报告需可阅读结构和实质内容；Excel需实际数据、合理列名及需要的公式。最终验收检查实际成果存在、可打开/预览、页数/行数及正文要求；不为文件任务启动Web服务或反复检查生成脚本每行代码。
任务验证可以用run_shell真正生成/打开检查成果，关联requirement_ids，使用返回的verification_id更新任务；平台最终独立验证实际文件与预览。所有明确需求必须落实到成果，不以命令成功代替内容完成。
'''


def validate_deliverables(plan):
    intent = plan.get('task_type', 'software' if plan['application_type'] != 'artifact' else 'other')
    if intent not in INTENTS:
        raise ValueError('task_type无效')
    values = plan.get('deliverables', [])
    if not isinstance(values, list) or len(values) > 40:
        raise ValueError('deliverables必须为0–40项文件成果列表')
    if plan['application_type'] == 'artifact' and not values:
        raise ValueError('文件任务必须规划真实deliverables')
    ids = {r['id'] for r in plan['requirements']}
    seen, covered = set(), set()
    for item in values:
        if not isinstance(item, dict):
            raise ValueError('每个成果必须为对象')
        name = item.get('path'); fmt = item.get('format')
        if (not isinstance(name, str) or len(name)>1000 or '\\' in name or
            Path(name).is_absolute() or any(p.startswith('.') for p in Path(name).parts) or
            len(Path(name).parts)<2 or Path(name).parts[0] != 'dist' or name in seen):
            raise ValueError('成果path必须是dist内唯一非隐藏相对文件路径')
        if fmt not in FORMATS or Path(name).suffix.lower() != '.'+fmt:
            raise ValueError('成果format必须受支持且与文件扩展名一致')
        if not isinstance(item.get('title'), str) or not item['title'].strip():
            raise ValueError('成果必须提供title')
        refs = item.get('requirement_ids')
        if not isinstance(refs,list) or not refs or not all(isinstance(v,str) and v in ids for v in refs):
            raise ValueError('成果requirement_ids必须关联当前需求')
        units = item.get('min_units',1)
        if type(units) is not int or not 1 <= units <= 10000:
            raise ValueError('成果min_units必须为1–10000整数')
        checks = item.get('content_checks',[])
        if not isinstance(checks,list) or len(checks)>40 or not all(isinstance(v,str) and 1<=len(v.strip())<=200 for v in checks):
            raise ValueError('成果content_checks必须为0–40个1–200字符的正文片段')
        if fmt in {'pptx','ppt','pdf','docx','doc','xlsx','xls','csv','md','txt','html'} and not checks:
            raise ValueError('文本或表格成果必须至少提供一个正文content_checks')
        seen.add(name); covered.update(refs)
    required = {r['id'] for r in plan['requirements'] if r['verification']=='artifact'}
    if not required.issubset(covered):
        raise ValueError('artifact需求未关联实际文件成果')
    if plan['application_type']=='artifact' and any(r['verification']!='artifact' for r in plan['requirements']):
        raise ValueError('纯文件任务需求verification必须为artifact')
    return plan
