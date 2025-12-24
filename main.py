import random
import asyncio
from datetime import datetime, time, timedelta
from typing import List, Dict, Tuple, Optional, Set, Any, Union
import json
from pathlib import Path

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, StarTools
from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.filter.permission import PermissionType
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.job import Job
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
import zoneinfo


class TimeSlot:
    """时间段类"""
    def __init__(self, name: str, time_range: str):
        self.name = name  # morning, noon, evening
        self.time_range = time_range
        self.start_time: Optional[time] = None
        self.end_time: Optional[time] = None
        self.chatted_today: Set[str] = set()  # 今天已发言的群ID集合
        self._parse_time_range()
    
    def _parse_time_range(self):
        """解析时间范围"""
        if not self.time_range or self.time_range.strip() == "":
            return
        
        try:
            if "-" in self.time_range:
                start_str, end_str = self.time_range.split("-")
                start_hour, start_minute = map(int, start_str.split(":"))
                end_hour, end_minute = map(int, end_str.split(":"))
                
                self.start_time = time(start_hour, start_minute)
                self.end_time = time(end_hour, end_minute)
        except Exception as e:
            logger.error(f"解析时间段 {self.name}:{self.time_range} 失败: {e}")
    
    def is_enabled(self) -> bool:
        """检查时间段是否启用"""
        return self.start_time is not None and self.end_time is not None
    
    def is_time_in_slot(self, current_time: time) -> bool:
        """检查当前时间是否在时间段内"""
        if not self.is_enabled():
            return False
        
        # 处理跨午夜的时间段
        if self.start_time <= self.end_time:
            # 不跨午夜
            return self.start_time <= current_time <= self.end_time
        else:
            # 跨午夜
            return current_time >= self.start_time or current_time <= self.end_time
    
    def has_chatted_today(self, group_id: str) -> bool:
        """检查今天是否已经在该时间段发言过"""
        return group_id in self.chatted_today
    
    def mark_as_chatted(self, group_id: str):
        """标记为今天已发言"""
        self.chatted_today.add(group_id)
    
    def reset_daily_chat(self):
        """重置每日发言记录"""
        self.chatted_today.clear()
    
    def get_time_range_str(self) -> str:
        """获取时间范围字符串"""
        if self.is_enabled():
            return f"{self.start_time.strftime('%H:%M')}-{self.end_time.strftime('%H:%M')}"
        return "未启用"


class GroupClientInfo:
    """群组与客户端关联信息"""
    def __init__(self, group_id: str, platform_type: str, platform_name: str = ""):
        self.group_id = group_id
        self.platform_type = platform_type  # 平台类型
        self.platform_name = platform_name  # 平台名称
        self.last_checked = datetime.now()  # 最后检查时间
        self.is_active = True  # 群组是否活跃（机器人是否在群中）
        self._client_getter = None  # 客户端获取函数

    def set_client_getter(self, getter_func):
        """设置客户端获取函数"""
        self._client_getter = getter_func

    async def get_client(self):
        """动态获取客户端实例"""
        if self._client_getter:
            return await self._client_getter()
        return None


class AutoGroupChat(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        
        # 时区设置 - 最先初始化，因为其他方法可能会用到
        tz = self.context.get_config().get("timezone")
        self.timezone = zoneinfo.ZoneInfo(tz) if tz else zoneinfo.ZoneInfo("Asia/Shanghai")
        
        # 加载配置
        self.enabled_groups: List[str] = config.get("enabled_groups", [])
        
        # 时间段配置
        time_slots_config = config.get("time_slots", {
            "morning": "06:40-08:15",
            "noon": "11:30-13:20",
            "evening": "19:20-22:15"
        })
        
        # 创建时间段对象
        self.time_slots: Dict[str, TimeSlot] = {
            "morning": TimeSlot("morning", time_slots_config.get("morning", "")),
            "noon": TimeSlot("noon", time_slots_config.get("noon", "")),
            "evening": TimeSlot("evening", time_slots_config.get("evening", ""))
        }
        
        # LLM配置
        self.use_llm: bool = config.get("use_llm", False)
        self.llm_provider_id: str = config.get("llm_provider_id", "")
        
        # 分时段提示词和消息
        self.morning_prompts: List[str] = config.get("morning_prompts", [])
        self.noon_prompts: List[str] = config.get("noon_prompts", [])
        self.evening_prompts: List[str] = config.get("evening_prompts", [])
        
        self.morning_messages: List[str] = config.get("morning_messages", [])
        self.noon_messages: List[str] = config.get("noon_messages", [])
        self.evening_messages: List[str] = config.get("evening_messages", [])
        
        # 发言控制 - 改为秒为单位
        self.group_cooldown: int = config.get("group_cooldown", 300)  # 默认5分钟=300秒
        
        # 打卡配置
        self.enable_group_checkin: bool = config.get("enable_group_checkin", True)
        self.checkin_time: str = config.get("checkin_time", "08:00")
        
        # 其他配置
        self.log_enabled: bool = config.get("log_enabled", True)
        
        # 发言记录
        self.last_group_chat_time: Optional[datetime] = None  # 上次群发言时间（全局）
        self.day_count: int = 1  # 打卡天数计数
        self.last_reset_date: str = ""  # 上次重置日期
        self.checkin_history: List[Dict] = []  # 打卡历史记录
        self.chat_history: List[Dict] = []  # 发言历史记录
        
        # 群组-客户端映射缓存（解决多账号支持问题）
        self.group_client_map: Dict[str, GroupClientInfo] = {}
        
        # 数据存储
        data_dir = StarTools.get_data_dir("astrbot_plugin_furry_maopao")
        self.data_path = data_dir / "auto_chat_data.json"
        self.data_path.parent.mkdir(parents=True, exist_ok=True)
        
        # 调度器
        self.scheduler = AsyncIOScheduler(timezone=self.timezone)
        
        # 定时任务
        self.checkin_job: Optional[Job] = None
        self.chat_job: Optional[Job] = None
        self.reset_job: Optional[Job] = None

    async def initialize(self):
        """初始化插件（异步）"""
        # 加载数据
        self._load_data()
        
        # 启动定时任务
        self._setup_scheduler()
        self.scheduler.start()
        
        logger.info(f"🤖 自动群打卡发言插件初始化完成 v1.3.5")
        logger.info(f"⏰ 时间段配置:")
        for slot_name, slot in self.time_slots.items():
            if slot.is_enabled():
                logger.info(f"  ✅ {slot_name}: {slot.get_time_range_str()}")
            else:
                logger.info(f"  ❌ {slot_name}: 未启用")
        logger.info(f"🤖 使用LLM: {self.use_llm}")
        logger.info(f"📅 打卡天数: {self.day_count}")
        logger.info(f"⏱️ 群间冷却: {self.group_cooldown}秒 ({self.group_cooldown/60:.1f}分钟)")
        logger.info(f"✅ 打卡设置: {'已启用' if self.enable_group_checkin else '已禁用'}")
        if self.enable_group_checkin and self.checkin_time:
            logger.info(f"⏰ 打卡时间: {self.checkin_time}（仅调用API，不发送消息）")
        
        # 记录各时段已发言群数
        for slot_name, slot in self.time_slots.items():
            if slot.is_enabled():
                logger.info(f"📊 {slot_name}时段已发言群数: {len(slot.chatted_today)}")

    def _load_data(self):
        """加载存储数据"""
        try:
            if self.data_path.exists():
                with self.data_path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                    
                    # 加载时间段发言记录
                    for slot_name, slot in self.time_slots.items():
                        chatted_groups = data.get(f"{slot_name}_chatted", [])
                        slot.chatted_today = set(chatted_groups)
                    
                    # 加载上次发言时间
                    last_group_time = data.get("last_group_chat_time")
                    if last_group_time:
                        dt = datetime.fromisoformat(last_group_time)
                        if dt.tzinfo is None:
                            self.last_group_chat_time = dt.replace(tzinfo=self.timezone)
                        else:
                            self.last_group_chat_time = dt.astimezone(self.timezone)
                    
                    # 加载其他数据
                    self.day_count = data.get("day_count", 1)
                    self.last_reset_date = data.get("last_reset_date", "")
                    
                    # 加载历史记录并确保格式正确
                    self.checkin_history = self._normalize_history(data.get("checkin_history", []))
                    self.chat_history = self._normalize_history(data.get("chat_history", []))
                    
                    # 检查是否需要重置每日发言记录
                    self._check_and_reset_daily_chat()
                    
                    logger.info(f"📊 已加载历史数据：打卡天数={self.day_count}, "
                               f"历史记录={len(self.checkin_history)}条, "
                               f"发言记录={len(self.chat_history)}条")
            else:
                self._reset_daily_chat_data()
                logger.info("📊 初始化新数据文件")
        except Exception as e:
            logger.error(f"加载数据失败: {e}")
            self._reset_daily_chat_data()

    def _normalize_history(self, history: List[Dict]) -> List[Dict]:
        """规范化历史记录数据"""
        normalized = []
        for record in history:
            # 确保每个记录都有timestamp字段
            if "timestamp" not in record:
                # 如果没有timestamp，添加一个默认值
                record["timestamp"] = datetime.now(self.timezone).isoformat()
            normalized.append(record)
        return normalized

    def _save_data(self):
        """保存数据"""
        try:
            # 转换时间为ISO格式
            last_group_time = None
            if self.last_group_chat_time:
                dt = self.last_group_chat_time.astimezone(self.timezone)
                last_group_time = dt.isoformat()
            
            data = {
                "last_group_chat_time": last_group_time,
                "day_count": self.day_count,
                "last_reset_date": self.last_reset_date,
                "checkin_history": self.checkin_history[-100:],  # 只保留最近100条记录
                "chat_history": self.chat_history[-100:]  # 只保留最近100条发言记录
            }
            
            # 保存时间段发言记录
            for slot_name, slot in self.time_slots.items():
                data[f"{slot_name}_chatted"] = list(slot.chatted_today)
            
            with self.data_path.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.debug("数据已保存")
        except Exception as e:
            logger.error(f"保存数据失败: {e}")

    def _check_and_reset_daily_chat(self):
        """检查并重置每日发言记录"""
        now = datetime.now(self.timezone)
        today = now.date().strftime("%Y-%m-%d")
        
        if self.last_reset_date != today:
            logger.info(f"📅 检测到日期变化 {self.last_reset_date} -> {today}，重置每日发言记录")
            
            # 记录重置前的状态
            old_stats = {}
            for slot_name, slot in self.time_slots.items():
                if slot.is_enabled():
                    old_stats[slot_name] = len(slot.chatted_today)
            
            self._reset_daily_chat_data()
            self.last_reset_date = today
            
            # 记录发言历史
            if old_stats:
                history_entry = {
                    "timestamp": now.isoformat(),
                    "date": self.last_reset_date,
                    "old_stats": old_stats,
                    "message": "每日重置",
                    "type": "reset"
                }
                self.chat_history.append(history_entry)
            
            self._save_data()

    def _reset_daily_chat_data(self):
        """重置每日发言数据"""
        for slot in self.time_slots.values():
            slot.reset_daily_chat()

    def _setup_scheduler(self):
        """设置定时任务"""
        # 设置打卡任务
        if self.enable_group_checkin and self.checkin_time and self.checkin_time.strip():
            try:
                checkin_hour, checkin_minute = map(int, self.checkin_time.split(":"))
                self.checkin_job = self.scheduler.add_job(
                    self._execute_checkin,
                    trigger=CronTrigger(
                        hour=checkin_hour,
                        minute=checkin_minute,
                        second=0,
                        timezone=self.timezone
                    ),
                    name="group_checkin_daily",
                    misfire_grace_time=300,
                )
                logger.info(f"✅ 群打卡任务已设置: {self.checkin_time}")
            except Exception as e:
                logger.error(f"设置打卡任务失败: {e}")
        else:
            logger.info("❌ 群打卡功能未启用或未配置时间")
        
        # 设置每日重置任务（每天0点重置发言记录）
        self.reset_job = self.scheduler.add_job(
            self._reset_daily_chat,
            trigger=CronTrigger(
                hour=0,
                minute=0,
                second=0,
                timezone=self.timezone
            ),
            name="reset_daily_chat",
            misfire_grace_time=300,
        )
        logger.info("✅ 每日重置任务已设置（0点重置发言记录）")
        
        # 设置发言任务
        self._setup_chat_scheduler()
        
        # 立即执行一次检查
        asyncio.create_task(self._check_and_chat())

    def _setup_chat_scheduler(self):
        """设置发言调度器"""
        if self.chat_job:
            self.chat_job.remove()
        
        # 每分钟检查一次是否需要发言
        self.chat_job = self.scheduler.add_job(
            self._check_and_chat,
            trigger=IntervalTrigger(minutes=1),
            name="auto_chat_check",
            misfire_grace_time=60,
        )
        logger.info("✅ 发言检查任务已设置（每分钟检查一次）")

    async def _check_and_chat(self):
        """检查并执行发言"""
        try:
            now = datetime.now(self.timezone)
            current_time = now.time()
            
            # 检查日期变化
            self._check_and_reset_daily_chat()
            
            # 检查全局冷却时间
            if self.last_group_chat_time:
                time_since_last_group = (now - self.last_group_chat_time).total_seconds()
                if time_since_last_group < self.group_cooldown:
                    remaining = int(self.group_cooldown - time_since_last_group)
                    logger.debug(f"冷却中，剩余 {remaining} 秒")
                    return
            
            # 获取所有启用的群组
            groups_to_chat = await self._get_active_groups()
            if not groups_to_chat:
                logger.debug("没有找到启用的群组")
                return
            
            # 找出当前处于哪个时间段
            current_slot = None
            for slot_name, slot in self.time_slots.items():
                if slot.is_enabled() and slot.is_time_in_slot(current_time):
                    current_slot = slot
                    break
            
            if not current_slot:
                logger.debug("当前不在任何时间段内")
                return
            
            logger.info(f"🕒 当前处于 {current_slot.name} 时段 ({current_slot.get_time_range_str()})")
            
            # 找出尚未在该时间段发言的群组
            available_groups = []
            for group_id in groups_to_chat:
                if not current_slot.has_chatted_today(group_id):
                    available_groups.append(group_id)
            
            logger.info(f"📊 群组统计: 总群数={len(groups_to_chat)}, 可发言={len(available_groups)}")
            
            if not available_groups:
                logger.info(f"✅ {current_slot.name}时段所有群组都已发言过")
                return
            
            # 随机选择一个群
            selected_group = random.choice(available_groups)
            logger.info(f"🎯 选择群 {selected_group} 在 {current_slot.name} 时段发言")
            
            # 执行发言
            await self._execute_chat_for_group(selected_group, current_slot)
        
        except Exception as e:
            logger.error(f"检查发言失败: {e}")

    async def _get_active_groups(self) -> List[str]:
        """获取活跃群组列表"""
        try:
            platforms = self.context.platform_manager.get_insts()
            active_groups = []
            current_time = datetime.now()
            
            # 清理过期的缓存
            await self._cleanup_expired_groups(current_time)
            
            for platform in platforms:
                try:
                    await self._process_platform_groups(platform, active_groups, current_time)
                except Exception as e:
                    logger.error(f"处理平台 {platform.__class__.__name__} 群组失败: {e}")
                    continue
            
            # 统计活跃和不活跃的群组
            await self._log_group_statistics()
            
            return list(set(active_groups))  # 去重
        except Exception as e:
            logger.error(f"获取活跃群组失败: {e}")
            return []

    async def _cleanup_expired_groups(self, current_time: datetime):
        """清理过期的群组缓存"""
        expired_groups = []
        inactive_groups = []
        
        for group_id, info in list(self.group_client_map.items()):
            # 检查是否过期（1小时）
            if (current_time - info.last_checked).total_seconds() > 3600:
                expired_groups.append(group_id)
            # 检查是否不活跃
            elif not info.is_active:
                inactive_groups.append(group_id)
        
        for group_id in expired_groups + inactive_groups:
            if group_id in self.group_client_map:
                del self.group_client_map[group_id]

    async def _process_platform_groups(self, platform, active_groups: List[str], current_time: datetime):
        """处理单个平台的群组"""
        if hasattr(platform, 'get_client'):
            client = platform.get_client()
            if client:
                platform_type = platform.__class__.__name__
                platform_name = getattr(platform, 'name', platform_type)
                
                # 获取群列表
                groups = await client.get_group_list()
                
                # 当前平台所有群组的集合
                current_platform_groups = set()
                
                for group in groups:
                    group_id = str(group.get('group_id', ''))
                    
                    # 检查是否在启用列表中
                    if not self.enabled_groups or group_id in self.enabled_groups:
                        active_groups.append(group_id)
                        current_platform_groups.add(group_id)
                        
                        # 缓存群组信息
                        await self._cache_group_info(
                            group_id, platform_type, platform_name, client, current_time
                        )
                
                # 标记不在当前群列表中的群组为不活跃
                await self._mark_inactive_groups(platform_type, current_platform_groups)

    async def _cache_group_info(self, group_id: str, platform_type: str, 
                               platform_name: str, client, current_time: datetime):
        """缓存群组信息"""
        if group_id not in self.group_client_map:
            info = GroupClientInfo(group_id, platform_type, platform_name)
            
            # 创建客户端获取函数
            def create_client_getter(c=client):
                async def get_client():
                    return c
                return get_client
            
            info.set_client_getter(create_client_getter())
            info.last_checked = current_time
            info.is_active = True
            self.group_client_map[group_id] = info
        else:
            # 更新最后检查时间和活跃状态
            info = self.group_client_map[group_id]
            info.last_checked = current_time
            info.is_active = True

    async def _mark_inactive_groups(self, platform_type: str, current_platform_groups: Set[str]):
        """标记不在当前群列表中的群组为不活跃"""
        for group_id, info in list(self.group_client_map.items()):
            if info.platform_type == platform_type and group_id not in current_platform_groups:
                info.is_active = False

    async def _log_group_statistics(self):
        """记录群组统计信息"""
        active_count = len([info for info in self.group_client_map.values() if info.is_active])
        inactive_count = len([info for info in self.group_client_map.values() if not info.is_active])
        
        logger.info(f"📊 群组统计: 活跃={active_count}个, "
                   f"不活跃={inactive_count}个, "
                   f"缓存总数={len(self.group_client_map)}个")

    async def _verify_group_active(self, group_id: str) -> bool:
        """验证群组是否活跃"""
        try:
            if group_id in self.group_client_map:
                info = self.group_client_map[group_id]
                
                if not info.is_active:
                    return False
                
                client = await info.get_client()
                if not client:
                    info.is_active = False
                    return False
                
                try:
                    # 尝试获取群信息
                    if hasattr(client, 'get_group_info'):
                        group_info = await client.get_group_info(group_id=int(group_id))
                        if group_info:
                            info.is_active = True
                            info.last_checked = datetime.now()
                            return True
                    
                    # 检查群列表是否存在该群
                    if hasattr(client, 'get_group_list'):
                        groups = await client.get_group_list()
                        group_ids = [str(g.get('group_id', '')) for g in groups]
                        if group_id in group_ids:
                            info.is_active = True
                            info.last_checked = datetime.now()
                            return True
                    
                    info.is_active = False
                    return False
                        
                except Exception:
                    info.is_active = False
                    return False
            
            # 如果缓存中没有，尝试重新获取
            client, platform_type = await self._get_client_for_group(group_id)
            return client is not None
                
        except Exception:
            if group_id in self.group_client_map:
                self.group_client_map[group_id].is_active = False
            return False

    async def _get_client_for_group(self, group_id: str) -> Tuple[Any, Optional[str]]:
        """获取指定群组的客户端"""
        try:
            # 首先尝试从缓存获取
            if group_id in self.group_client_map:
                info = self.group_client_map[group_id]
                if not info.is_active:
                    return None, None
                
                client = await info.get_client()
                if client:
                    return client, info.platform_type
            
            # 缓存中没有或无效，遍历平台查找
            platforms = self.context.platform_manager.get_insts()
            for platform in platforms:
                if hasattr(platform, 'get_client'):
                    client = platform.get_client()
                    if client:
                        try:
                            # 尝试获取群列表来验证客户端是否在该群
                            groups = await client.get_group_list()
                            for group in groups:
                                if str(group.get('group_id', '')) == group_id:
                                    # 找到匹配的客户端，缓存它
                                    platform_type = platform.__class__.__name__
                                    platform_name = getattr(platform, 'name', platform_type)
                                    
                                    info = GroupClientInfo(group_id, platform_type, platform_name)
                                    
                                    def create_client_getter(c=client):
                                        async def get_client():
                                            return c
                                        return get_client
                                    
                                    info.set_client_getter(create_client_getter())
                                    info.is_active = True
                                    self.group_client_map[group_id] = info
                                    
                                    return client, platform_type
                        except Exception:
                            continue
            
            # 没有找到，标记为不活跃
            if group_id in self.group_client_map:
                self.group_client_map[group_id].is_active = False
            
            return None, None
        except Exception as e:
            logger.error(f"获取群组 {group_id} 客户端失败: {e}")
            return None, None

    def _get_messages_for_slot(self, slot_name: str) -> List[str]:
        """获取对应时间段的预设消息"""
        if slot_name == "morning":
            return self.morning_messages
        elif slot_name == "noon":
            return self.noon_messages
        elif slot_name == "evening":
            return self.evening_messages
        return []

    def _get_prompts_for_slot(self, slot_name: str) -> List[str]:
        """获取对应时间段的提示词"""
        if slot_name == "morning":
            return self.morning_prompts
        elif slot_name == "noon":
            return self.noon_prompts
        elif slot_name == "evening":
            return self.evening_prompts
        return []

    def _get_random_message_for_slot(self, slot_name: str) -> str:
        """获取对应时间段的随机消息"""
        messages = self._get_messages_for_slot(slot_name)
        if messages:
            return random.choice(messages)
        
        # 默认消息
        if slot_name == "morning":
            return "大家早上好呀~新的一天开始了！"
        elif slot_name == "noon":
            return "中午好，大家吃饭了吗？"
        elif slot_name == "evening":
            return "晚上好，今天过得怎么样？"
        
        return "大家好，我来冒个泡~"

    async def _generate_llm_message_for_slot(self, slot_name: str) -> str:
        """使用LLM为对应时间段生成简洁消息"""
        try:
            if not self.llm_provider_id:
                return self._get_random_message_for_slot(slot_name)
            
            # 使用对应时间段的提示词
            prompts = self._get_prompts_for_slot(slot_name)
            prompt = random.choice(prompts) if prompts else f"生成一句简短的{slot_name}时段群聊发言，不超过20字"
            prompt += "，请生成简洁的回复，不超过20字，不要长篇大论"
            
            # 尝试调用LLM
            try:
                llm_resp = await self.context.llm_generate(
                    chat_provider_id=self.llm_provider_id or None,
                    prompt=prompt,
                    max_tokens=30,
                    temperature=0.7
                )
                if hasattr(llm_resp, 'completion_text'):
                    message = llm_resp.completion_text.strip()
                    message = self._cleanup_message(message)
                    return message
            except Exception:
                pass
            
            # 回退到预设消息
            return self._get_random_message_for_slot(slot_name)
            
        except Exception as e:
            logger.error(f"生成LLM消息失败: {e}")
            return self._get_random_message_for_slot(slot_name)

    def _cleanup_message(self, message: str) -> str:
        """清理消息，确保简洁"""
        message = message.strip()
        message = ' '.join(message.split())
        
        if len(message) > 50:
            message = message[:47] + "..."
        
        return message

    async def _send_group_message(self, client, group_id: str, message: str) -> bool:
        """发送群消息"""
        try:
            # 先验证群组是否活跃
            is_active = await self._verify_group_active(group_id)
            if not is_active:
                return False
            
            # 通用发送方法
            if hasattr(client, 'send_group_msg'):
                await client.send_group_msg(group_id=int(group_id), message=message)
            elif hasattr(client, 'send_message'):
                await client.send_message(group_id=int(group_id), message=message)
            else:
                logger.error(f"客户端不支持发送群消息: {type(client)}")
                return False
            
            # 更新发言时间
            now = datetime.now(self.timezone)
            self.last_group_chat_time = now
            
            if self.log_enabled:
                logger.info(f"💬 已发送群消息到 {group_id}: {message}")
            
            return True
        except Exception as e:
            logger.error(f"发送群消息失败: {e}")
            if group_id in self.group_client_map:
                self.group_client_map[group_id].is_active = False
            return False

    async def _execute_chat_for_group(self, group_id: str, time_slot: TimeSlot):
        """为指定群组在指定时间段执行发言"""
        try:
            # 再次检查是否已发言
            if time_slot.has_chatted_today(group_id):
                return
            
            # 验证群组是否活跃
            is_active = await self._verify_group_active(group_id)
            if not is_active:
                if group_id in time_slot.chatted_today:
                    time_slot.chatted_today.remove(group_id)
                return
            
            # 获取消息内容
            if self.use_llm:
                message = await self._generate_llm_message_for_slot(time_slot.name)
            else:
                message = self._get_random_message_for_slot(time_slot.name)
            
            logger.info(f"📤 准备发送消息到群 {group_id} ({time_slot.name}时段): {message}")
            
            # 获取正确的客户端
            client, platform_type = await self._get_client_for_group(group_id)
            if not client:
                return
            
            # 发送消息
            success = await self._send_group_message(client, group_id, message)
            if success:
                # 标记为已发言
                time_slot.mark_as_chatted(group_id)
                
                # 记录发言历史
                now = datetime.now(self.timezone)
                chat_record = {
                    "timestamp": now.isoformat(),
                    "group_id": group_id,
                    "slot": time_slot.name,
                    "platform": platform_type,
                    "message": message,
                    "success": True
                }
                self.chat_history.append(chat_record)
                
                self._save_data()
                logger.info(f"✅ 群 {group_id} {time_slot.name}时段发言完成")
            else:
                logger.error(f"❌ 群 {group_id} 发言发送失败")
        
        except Exception as e:
            logger.error(f"执行群 {group_id} 发言失败: {e}")

    async def _execute_checkin(self):
        """执行群打卡"""
        if not self.enable_group_checkin or not self.checkin_time:
            return
        
        try:
            now = datetime.now(self.timezone)
            logger.info(f"⏰ 开始执行群打卡，时间: {now.strftime('%Y-%m-%d %H:%M:%S')}")
            
            # 获取所有启用的群组
            groups_to_checkin = await self._get_active_groups()
            
            if not groups_to_checkin:
                return
            
            success_count = 0
            failed_groups = []
            
            for group_id in groups_to_checkin:
                try:
                    # 验证群组是否活跃
                    is_active = await self._verify_group_active(group_id)
                    if not is_active:
                        continue
                    
                    # 调用打卡API
                    api_success = await self._execute_group_checkin(group_id)
                    if api_success:
                        success_count += 1
                        
                        # 记录打卡历史
                        checkin_record = {
                            "timestamp": now.isoformat(),
                            "group_id": group_id,
                            "success": True
                        }
                        self.checkin_history.append(checkin_record)
                    else:
                        failed_groups.append(group_id)
                        
                        # 记录失败历史
                        checkin_record = {
                            "timestamp": now.isoformat(),
                            "group_id": group_id,
                            "success": False,
                            "error": "API调用失败"
                        }
                        self.checkin_history.append(checkin_record)
                
                except Exception as e:
                    failed_groups.append(group_id)
                    checkin_record = {
                        "timestamp": now.isoformat(),
                        "group_id": group_id,
                        "success": False,
                        "error": str(e)
                    }
                    self.checkin_history.append(checkin_record)
            
            # 更新打卡天数
            self.day_count += 1
            self._save_data()
            
            logger.info(f"✅ 群打卡完成，成功调用 {success_count}/{len(groups_to_checkin)} 个群的打卡API")
            
            if failed_groups:
                logger.warning(f"❌ 以下群组打卡失败: {failed_groups}")
            
        except Exception as e:
            logger.error(f"执行群打卡失败: {e}")

    async def _execute_group_checkin(self, group_id: str) -> bool:
        """执行群打卡API调用"""
        try:
            # 获取正确的客户端
            client, platform_type = await self._get_client_for_group(group_id)
            if not client:
                return False
            
            # 使用鸭子类型检测是否支持打卡API
            api_supported = await self._check_checkin_api_support(client)
            if not api_supported:
                return False
            
            # 尝试多种API调用方式
            try:
                # 方法1：直接调用send_group_sign方法
                if hasattr(client, 'send_group_sign'):
                    ret = await client.send_group_sign(group_id=int(group_id))
                    return self._check_checkin_result(ret)
                
                # 方法2：使用api对象调用
                if hasattr(client, 'api'):
                    if hasattr(client.api, 'send_group_sign'):
                        ret = await client.api.send_group_sign(group_id=int(group_id))
                        return self._check_checkin_result(ret)
                    
                    if hasattr(client.api, 'call_action'):
                        ret = await client.api.call_action('send_group_sign', group_id=int(group_id))
                        return self._check_checkin_result(ret)
                
                return False
                
            except Exception:
                return False
                
        except Exception:
            return False

    async def _check_checkin_api_support(self, client) -> bool:
        """检查客户端是否支持打卡API"""
        try:
            if hasattr(client, 'send_group_sign'):
                return True
            
            if hasattr(client, 'api'):
                if hasattr(client.api, 'send_group_sign'):
                    return True
                
                if hasattr(client.api, 'call_action'):
                    return True
            
            return False
        except Exception:
            return False

    def _check_checkin_result(self, result: Any) -> bool:
        """检查打卡API返回结果"""
        if result is None:
            return False
        
        if isinstance(result, dict):
            if 'retcode' in result and result['retcode'] == 0:
                return True
            elif 'status' in result and result['status'] == 'ok':
                return True
            elif 'data' in result and isinstance(result['data'], dict):
                return result.get('status') == 'ok'
            else:
                return False
        else:
            if isinstance(result, bool):
                return result
            elif isinstance(result, str):
                return result.lower() in ['ok', 'success', 'true']
            else:
                return True

    async def _reset_daily_chat(self):
        """重置每日发言记录（定时任务）"""
        logger.info("🔄 正在重置每日发言记录...")
        self._reset_daily_chat_data()
        
        now = datetime.now(self.timezone)
        self.last_reset_date = now.date().strftime("%Y-%m-%d")
        self._save_data()
        
        logger.info("✅ 每日发言记录已重置")

    # ==================== 管理员命令 ====================

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("立即发言")
    async def immediate_chat(self, event: AstrMessageEvent):
        """立即在指定群发言"""
        try:
            group_id = event.get_group_id()
            if not group_id:
                yield event.plain_result("❌ 请在群聊中使用此命令")
                return
            
            now = datetime.now(self.timezone)
            current_time = now.time()
            
            # 找出当前处于哪个时间段
            current_slot = None
            for slot_name, slot in self.time_slots.items():
                if slot.is_enabled() and slot.is_time_in_slot(current_time):
                    current_slot = slot
                    break
            
            if not current_slot:
                yield event.plain_result("❌ 当前不在任何有效时间段内")
                return
            
            # 检查是否已发言
            if current_slot.has_chatted_today(group_id):
                yield event.plain_result(f"❌ 今天已在 {current_slot.name} 时段发言过")
                return
            
            # 检查冷却时间
            if self.last_group_chat_time:
                time_since_last = (now - self.last_group_chat_time).total_seconds()
                if time_since_last < self.group_cooldown:
                    remaining = int(self.group_cooldown - time_since_last)
                    yield event.plain_result(f"❌ 冷却中，请等待 {remaining} 秒")
                    return
            
            # 验证群组是否活跃
            is_active = await self._verify_group_active(group_id)
            if not is_active:
                yield event.plain_result(f"❌ 群组 {group_id} 不活跃（机器人可能已退出该群）")
                return
            
            yield event.plain_result("🔄 正在生成发言内容...")
            
            # 获取消息内容
            if self.use_llm:
                message = await self._generate_llm_message_for_slot(current_slot.name)
            else:
                message = self._get_random_message_for_slot(current_slot.name)
            
            # 获取客户端并发送消息
            client, platform_type = await self._get_client_for_group(group_id)
            if not client:
                yield event.plain_result("❌ 找不到群组的客户端，无法发送消息")
                return
            
            success = await self._send_group_message(client, group_id, message)
            
            if success:
                # 标记为已发言
                current_slot.mark_as_chatted(group_id)
                
                # 记录发言历史
                chat_record = {
                    "timestamp": now.isoformat(),
                    "group_id": group_id,
                    "slot": current_slot.name,
                    "platform": platform_type,
                    "message": message,
                    "success": True,
                    "manual": True
                }
                self.chat_history.append(chat_record)
                
                self._save_data()
                yield event.plain_result(f"✅ 已发送发言 ({current_slot.name}时段): {message}")
            else:
                yield event.plain_result("❌ 发送失败")
                
        except Exception as e:
            logger.error(f"立即发言失败: {e}")
            yield event.plain_result(f"❌ 发言失败: {e}")

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("立即打卡")
    async def immediate_checkin(self, event: AstrMessageEvent):
        """立即执行打卡"""
        try:
            if not self.enable_group_checkin:
                yield event.plain_result("❌ 群打卡功能未启用")
                return
            
            group_id = event.get_group_id()
            if not group_id:
                yield event.plain_result("❌ 请在群聊中使用此命令")
                return
            
            # 验证群组是否活跃
            is_active = await self._verify_group_active(group_id)
            if not is_active:
                yield event.plain_result(f"❌ 群组 {group_id} 不活跃")
                return
            
            # 检查平台是否支持打卡
            client, platform_type = await self._get_client_for_group(group_id)
            if not client:
                yield event.plain_result("❌ 找不到群组的客户端")
                return
            
            api_supported = await self._check_checkin_api_support(client)
            if not api_supported:
                yield event.plain_result(f"❌ 当前平台不支持打卡功能")
                return
            
            yield event.plain_result("🔄 正在调用打卡API...")
            
            # 调用群打卡API
            api_success = await self._execute_group_checkin(group_id)
            
            if api_success:
                # 更新打卡天数和记录历史
                self.day_count += 1
                
                now = datetime.now(self.timezone)
                checkin_record = {
                    "timestamp": now.isoformat(),
                    "group_id": group_id,
                    "success": True,
                    "manual": True
                }
                self.checkin_history.append(checkin_record)
                
                self._save_data()
                
                yield event.plain_result(f"✅ 打卡API调用成功！当前天数: {self.day_count}")
            else:
                yield event.plain_result("❌ 打卡API调用失败")
            
        except Exception as e:
            logger.error(f"立即打卡失败: {e}")
            yield event.plain_result(f"❌ 打卡失败: {e}")

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("发言状态")
    async def chat_status(self, event: AstrMessageEvent):
        """查看插件状态"""
        try:
            now = datetime.now(self.timezone)
            current_time = now.time()
            today = now.date().strftime("%Y-%m-%d")
            
            # 获取活跃群组
            active_groups = await self._get_active_groups()
            
            status_info = f"🤖 自动发言插件状态 v1.3.5\n"
            status_info += f"⏰ 当前时间: {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
            status_info += f"📅 打卡天数: {self.day_count}\n"
            status_info += f"🔧 使用LLM: {'✅ 已开启' if self.use_llm else '❌ 未开启'}\n"
            status_info += f"❄️ 群间冷却: {self.group_cooldown}秒 ({self.group_cooldown/60:.1f}分钟)\n"
            status_info += f"✅ 群打卡: {'已开启' if self.enable_group_checkin else '已关闭'}\n"
            if self.enable_group_checkin and self.checkin_time:
                status_info += f"⏰ 打卡时间: {self.checkin_time}（仅调用API）\n"
            
            # 显示群组统计
            active_count = len([info for info in self.group_client_map.values() if info.is_active])
            inactive_count = len([info for info in self.group_client_map.values() if not info.is_active])
            status_info += f"📊 群组统计: 活跃={active_count}个, "
            status_info += f"不活跃={inactive_count}个, "
            status_info += f"缓存总数={len(self.group_client_map)}个\n"
            status_info += f"📊 当前活跃群组: {len(active_groups)}个\n"
            
            # 显示冷却状态
            if self.last_group_chat_time:
                time_since_last = (now - self.last_group_chat_time).total_seconds()
                if time_since_last < self.group_cooldown:
                    remaining = int(self.group_cooldown - time_since_last)
                    status_info += f"❄️ 冷却剩余: {remaining}秒\n"
                else:
                    status_info += f"✅ 冷却完成，可发言\n"
            
            # 显示时间段状态
            status_info += f"\n📅 时间段状态 (今日 {today}):\n"
            current_slot_name = None
            
            for slot_name, slot in self.time_slots.items():
                if slot.is_enabled():
                    in_slot = slot.is_time_in_slot(current_time)
                    status = "✅ 当前时段" if in_slot else "⏰ 未到"
                    if in_slot:
                        current_slot_name = slot_name
                    
                    chatted_count = len(slot.chatted_today)
                    status_info += f"  {slot_name}: {slot.get_time_range_str()} ({status})\n"
                    status_info += f"    已发言群数: {chatted_count} 个\n"
                else:
                    status_info += f"  {slot_name}: ❌ 未启用\n"
            
            # 显示当前时段详情
            if current_slot_name:
                current_slot = self.time_slots[current_slot_name]
                if active_groups:
                    available_count = len([g for g in active_groups if not current_slot.has_chatted_today(g)])
                    status_info += f"\n📊 {current_slot_name}时段详情:\n"
                    status_info += f"  总活跃群数: {len(active_groups)} 个\n"
                    status_info += f"  可发言群: {available_count} 个\n"
                    status_info += f"  已发言群: {len(current_slot.chatted_today)} 个\n"
            
            # 显示平台连接状态
            if self.group_client_map:
                platform_stats = {}
                for info in self.group_client_map.values():
                    if info.platform_type not in platform_stats:
                        platform_stats[info.platform_type] = {"active": 0, "inactive": 0}
                    if info.is_active:
                        platform_stats[info.platform_type]["active"] += 1
                    else:
                        platform_stats[info.platform_type]["inactive"] += 1
                
                status_info += f"\n📡 平台连接状态:\n"
                for platform, stats in platform_stats.items():
                    total = stats["active"] + stats["inactive"]
                    status_info += f"  {platform}: {total}个群组 (活跃:{stats['active']}, 不活跃:{stats['inactive']})\n"
            
            # 显示打卡历史（最近5条）
            if self.checkin_history:
                status_info += f"\n📝 最近打卡记录:\n"
                recent_history = self.checkin_history[-5:]  # 最近5条
                for record in reversed(recent_history):
                    # 安全地处理timestamp
                    timestamp_str = "未知时间"
                    if "timestamp" in record:
                        try:
                            dt = datetime.fromisoformat(record["timestamp"])
                            timestamp_str = dt.strftime("%m-%d %H:%M")
                        except (ValueError, TypeError):
                            timestamp_str = "时间格式错误"
                    
                    group_id = record.get("group_id", "未知群组")
                    success = "✅" if record.get("success") else "❌"
                    manual = "🔧" if record.get("manual") else "🤖"
                    status_info += f"  {timestamp_str} {manual} 群{group_id}: {success}\n"
            
            # 显示发言历史（最近5条）
            if self.chat_history:
                status_info += f"\n💬 最近发言记录:\n"
                recent_chats = self.chat_history[-5:]  # 最近5条
                for record in reversed(recent_chats):
                    # 安全地处理timestamp
                    timestamp_str = "未知时间"
                    if "timestamp" in record:
                        try:
                            dt = datetime.fromisoformat(record["timestamp"])
                            timestamp_str = dt.strftime("%m-%d %H:%M")
                        except (ValueError, TypeError):
                            timestamp_str = "时间格式错误"
                    
                    group_id = record.get("group_id", "未知群组")
                    slot = record.get("slot", "未知")
                    manual = "🔧" if record.get("manual") else "🤖"
                    status_info += f"  {timestamp_str} {manual} 群{group_id}({slot})\n"
            
            yield event.plain_result(status_info)
            
        except Exception as e:
            logger.error(f"获取状态失败: {e}")
            yield event.plain_result(f"❌ 获取状态失败: {e}")

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("重置发言记录")
    async def reset_chat_records(self, event: AstrMessageEvent):
        """重置发言记录"""
        try:
            old_stats = {}
            for slot_name, slot in self.time_slots.items():
                if slot.is_enabled():
                    old_stats[slot_name] = len(slot.chatted_today)
            
            self._reset_daily_chat_data()
            self.last_group_chat_time = None
            
            now = datetime.now(self.timezone)
            self.last_reset_date = now.date().strftime("%Y-%m-%d")
            
            # 记录手动重置历史
            history_entry = {
                "timestamp": now.isoformat(),
                "date": self.last_reset_date,
                "old_stats": old_stats,
                "message": "手动重置",
                "manual": True
            }
            self.chat_history.append(history_entry)
            
            self._save_data()
            
            response = "✅ 已重置所有发言记录\n"
            response += "📊 重置前状态:\n"
            for slot_name, count in old_stats.items():
                response += f"  {slot_name}: {count}个群已发言\n"
            
            yield event.plain_result(response)
            
        except Exception as e:
            logger.error(f"重置记录失败: {e}")
            yield event.plain_result(f"❌ 重置失败: {e}")

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("查看时段消息")
    async def view_slot_messages(self, event: AstrMessageEvent, slot_name: str = ""):
        """查看时段消息配置"""
        try:
            if slot_name and slot_name in ["morning", "noon", "evening"]:
                # 查看单个时段
                if self.use_llm:
                    prompts = self._get_prompts_for_slot(slot_name)
                    response = f"📝 {slot_name}时段提示词（共 {len(prompts)} 条）:\n"
                    for i, prompt in enumerate(prompts[:5], 1):
                        response += f"{i}. {prompt[:50]}...\n"
                    if len(prompts) > 5:
                        response += f"...还有 {len(prompts) - 5} 条未显示\n"
                else:
                    messages = self._get_messages_for_slot(slot_name)
                    response = f"📝 {slot_name}时段预设消息（共 {len(messages)} 条）:\n"
                    for i, msg in enumerate(messages[:5], 1):
                        response += f"{i}. {msg[:50]}...\n"
                    if len(messages) > 5:
                        response += f"...还有 {len(messages) - 5} 条未显示\n"
            else:
                # 查看所有时段
                response = "📋 各时段配置:\n"
                for slot_name in ["morning", "noon", "evening"]:
                    slot = self.time_slots[slot_name]
                    if self.use_llm:
                        prompts = self._get_prompts_for_slot(slot_name)
                        response += f"\n{slot_name} ({slot.get_time_range_str()}):\n"
                        response += f"  提示词数量: {len(prompts)} 条\n"
                        if prompts:
                            response += f"  示例: {prompts[0][:30]}...\n"
                    else:
                        messages = self._get_messages_for_slot(slot_name)
                        response += f"\n{slot_name} ({slot.get_time_range_str()}):\n"
                        response += f"  预设消息数量: {len(messages)} 条\n"
                        if messages:
                            response += f"  示例: {messages[0][:30]}...\n"
            
            yield event.plain_result(response)
            
        except Exception as e:
            logger.error(f"查看时段消息失败: {e}")
            yield event.plain_result(f"❌ 查看失败: {e}")

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("添加时段消息")
    async def add_slot_message(self, event: AstrMessageEvent, slot_name: str, *, content: str):
        """添加时段消息"""
        try:
            if slot_name not in ["morning", "noon", "evening"]:
                yield event.plain_result("❌ 时段名称错误\n💡 可用名称: morning, noon, evening")
                return
            
            if self.use_llm:
                # 添加提示词
                if slot_name == "morning":
                    self.morning_prompts.append(content)
                    self.config["morning_prompts"] = self.morning_prompts
                elif slot_name == "noon":
                    self.noon_prompts.append(content)
                    self.config["noon_prompts"] = self.noon_prompts
                elif slot_name == "evening":
                    self.evening_prompts.append(content)
                    self.config["evening_prompts"] = self.evening_prompts
            else:
                # 添加预设消息
                if slot_name == "morning":
                    self.morning_messages.append(content)
                    self.config["morning_messages"] = self.morning_messages
                elif slot_name == "noon":
                    self.noon_messages.append(content)
                    self.config["noon_messages"] = self.noon_messages
                elif slot_name == "evening":
                    self.evening_messages.append(content)
                    self.config["evening_messages"] = self.evening_messages
            
            self.config.save_config()
            
            yield event.plain_result(f"✅ 已为 {slot_name} 时段添加{'提示词' if self.use_llm else '预设消息'}")
            
        except Exception as e:
            logger.error(f"添加时段消息失败: {e}")
            yield event.plain_result(f"❌ 添加失败: {e}")

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("测试时段发言")
    async def test_slot_chat(self, event: AstrMessageEvent, slot_name: str):
        """测试时段发言"""
        try:
            if slot_name not in ["morning", "noon", "evening"]:
                yield event.plain_result("❌ 时段名称错误\n💡 可用名称: morning, noon, evening")
                return
            
            yield event.plain_result(f"🔄 正在生成 {slot_name} 时段发言内容...")
            
            # 获取消息内容
            if self.use_llm:
                message = await self._generate_llm_message_for_slot(slot_name)
            else:
                message = self._get_random_message_for_slot(slot_name)
            
            yield event.plain_result(f"💬 {slot_name}时段发言内容:\n{message}")
            
        except Exception as e:
            logger.error(f"测试时段发言失败: {e}")
            yield event.plain_result(f"❌ 测试失败: {e}")

    async def terminate(self):
        """插件卸载时清理资源"""
        try:
            if self.checkin_job:
                self.checkin_job.remove()
            if self.chat_job:
                self.chat_job.remove()
            if self.reset_job:
                self.reset_job.remove()
            self.scheduler.shutdown()
            self._save_data()
            logger.info("🛑 自动群打卡发言插件已停止")
        except Exception as e:
            logger.error(f"插件停止失败: {e}")