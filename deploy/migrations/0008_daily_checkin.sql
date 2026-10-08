-- 每日签到:记录最近一次签到的日期(北京时间的天数,从 1970-01-01 起算),同一天只发放一次
ALTER TABLE users ADD COLUMN checkin_day INTEGER NOT NULL DEFAULT 0;
