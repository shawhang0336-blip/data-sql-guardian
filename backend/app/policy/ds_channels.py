"""ds_agent BSP 命名数据通道声明（策略层 Layer 2）。"""

from typing import Dict

from app.kernel.run import ChannelSpec, append_list, last_value, merge_dict

PROJECT_ANALYSIS_CHANNELS: Dict[str, ChannelSpec] = {
    "script_findings": ChannelSpec("script_findings", append_list),
    "chunk_diagnostics": ChannelSpec("chunk_diagnostics", merge_dict),
    "project_findings": ChannelSpec("project_findings", append_list),
    "ai_roadmap": ChannelSpec("ai_roadmap", last_value),
}

CHANGE_REVIEW_CHANNELS: Dict[str, ChannelSpec] = {
    "diff_summary": ChannelSpec("diff_summary", last_value),
    "blast_radius": ChannelSpec("blast_radius", last_value),
    "gate_result": ChannelSpec("gate_result", last_value),
    "ai_review": ChannelSpec("ai_review", last_value),
}
