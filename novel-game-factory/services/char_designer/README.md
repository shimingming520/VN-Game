# AI 角色立绘与表情自动化生产流水线 (Character Image Pipeline)

这是一个基于 Python 的自动化流水线，用于从小说数据库中提取角色外貌特征，调用 AI (Qwen-Image) 生成高质量的角色立绘，并进一步基于立绘生成多套情绪表情差分图（自动抠图）。

## 📂 项目结构

本项目采用**绝对路径**策略，输出目录与代码目录平级。

假设代码存放在 `image_new` 目录：

```text
ProjectRoot/

├── outputs/                  # [自动生成] 产物输出目录
│   └── {task_id}/
│       └── images/
│           └── characters/   # 最终生成的立绘(.png)
├── image_new/                # 代码目录
    ├── .env                      # 环境变量配置文件
│   ├── main.py               # 🚀 程序主入口
│   ├── extract_characters_to_db.py        # Step 1: 角色数据提取
│   ├── character_image_generator_from_db.py # Step 2: 立绘生成
│   ├── emotion_image_generator.py         # Step 3: 表情变体生成
│   ├── requirements.txt
│   ├── log/                  # [自动生成] 运行日志
│   │   ├── step1_extract.log
│   │   ├── step2_char_gen.log
│   │   └── step3_emo_gen.log
│   └── src/                  # (自动下载) 抠图模型缓存
└── ...