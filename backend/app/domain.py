"""领域常量：事件码 -> 运单投影状态。独立于 seed / 模块，避免循环导入。"""

EVENT_STATUS = {
    "PICKED": "已揽收",
    "IN_TRANSIT": "运输中",
    "ARRIVED_HUB": "到站",
    "OUT_FOR_DELIVERY": "派送中",
    "DELIVERED": "已签收",
    "EXCEPTION": "异常",
}


def map_status(event_code: str) -> str:
    return EVENT_STATUS.get(event_code, "未知")
