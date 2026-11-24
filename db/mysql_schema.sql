-- FileMate 业务库（MySQL 8.0+）建表脚本
-- 与 SQLite 版字段保持一致；字符集统一 utf8mb4，便于存中文与 emoji。
-- 使用方式：python scripts/mysql_init.py

CREATE TABLE IF NOT EXISTS channels (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    name VARCHAR(255) NOT NULL,
    workspace_path VARCHAR(1024) NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS messages (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    channel_id BIGINT UNSIGNED NOT NULL,
    sender VARCHAR(16) NOT NULL COMMENT 'user 或 ai',
    content MEDIUMTEXT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_messages_channel (channel_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS logs (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    message MEDIUMTEXT NOT NULL,
    undo_info MEDIUMTEXT NULL COMMENT 'JSON 字符串，NULL 表示不可撤回',
    undone TINYINT NOT NULL DEFAULT 0 COMMENT '0 未撤回 / 1 已撤回',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_logs_undone (undone)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
