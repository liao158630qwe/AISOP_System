# 服务器首版运行与验收

当前实现支持上传视频的异步离线分析，以及浏览器摄像头的实时采样检测。原型文档中的RTSP工业相机、PLC、MES、正式SOP规则、SSE和生产事件inbox/outbox尚未实现；任务状态使用带超时的长轮询，不应按文档全量能力部署验收。

## 实时摄像头检测

主页面可先选择灯具工位，再开始实时检测或上传。上传前可选择离线分析姿态模型（默认RTMO-m，可选RTMPose-m或YOLO11s-pose；服务端默认由`POSE_BACKEND`决定）。六工位检查项、ROI/模板/专用模型配置与规则回放见[灯具算法与配置说明](station-algorithms.md)。更新本次算法后同时重启Web服务和离线Worker；Docker部署需要重新构建两者镜像，配置随镜像发布，模型挂载到`/app/models`。

更新代码后，在Web服务终端按Ctrl+C，再运行`npm start`。刷新主页面，在视频分析栏选择“实时摄像头检测”，登录后点击“开始检测”，允许摄像头权限。默认使用浏览器的电脑或USB摄像头；浏览器站点设置可调整摄像头。必须使用localhost、127.0.0.1或HTTPS访问。停止检测、切回视频回放、退出登录或离开页面会释放摄像头。

画面以JPEG采样发送到当前服务器，不存储实时视频或生成离线报告。视频分析栏提供“流畅预览”和“检测画面”两种视图：流畅预览直接显示摄像头视频；检测画面显示最近完成推理的采样帧及对应框和骨架。两者不混叠。可选5/10/15/30/60 fps检测目标，默认30；每个浏览器仅发送一个在途请求，以目标周期减去实际耗时安排下一帧，不积压旧画面。实际速率由摄像头、网络及推理耗时决定，预览帧率与检测帧率分别显示。通用姿态采样最大640×360，选择灯具工位时保留最大1280×720供小目标检测。繁忙（429）或短暂网络错误时最多自动重试5次，退避间隔0.5–8秒，并遵守服务端Retry-After（上限10秒）；持续失败、登录失效或无效画面时停止检测并显示原因。页面切到后台时暂停发送新采样帧，返回后恢复。单帧编号不代表跨帧身份，不支持行走时序判定，SOP仍待复核。

实时推理在API进程中运行，默认后端为RTMO-m（单阶段多人姿态，rtmlib + ONNX Runtime CUDA），可用`REALTIME_BACKEND`切换`rtmpose`/`yolo`；服务启动时在后台线程预热模型并跑热首帧（`REALTIME_WARMUP=false`可关闭），避免摄像头首帧等待数秒的CUDA图编译。`REALTIME_POOL_SIZE`控制有界模型槽位（默认2，范围1–4），槽位满载立即返回429，不无限排队；增加槽位前必须实测GPU显存和吞吐。离线Worker按任务记录的姿态后端加载并缓存模型（默认`POSE_BACKEND`，默认RTMO-m），任务创建时记录所选权重哈希，执行前校验模型未变化。API需要`requirements-worker.txt`及匹配的PyTorch；本机已有依赖。Compose的API镜像已改为CUDA运行时并申请GPU，需重新构建。每帧最多1 MiB、1280×720，最多50个人体检测。

## 1. Linux GPU服务器：PostgreSQL＋HTTPS

前提：Docker Engine、Docker Compose v2、NVIDIA驱动与NVIDIA Container Toolkit。GPU需与PyTorch 2.8/CUDA 12.8兼容。YOLO后端采用已有`models/yolo11s-pose.pt`；rtmlib后端（RTMO-m/RTMPose-m）需预置`~/.cache/rtmlib/hub/checkpoints`权重（随镜像或挂载提供），不在用户上传任务期间自动下载。

1. 复制`.env.example`为`.env`，设置独立的登录密码和数据库密码。登录密码至少12字符。
2. 设置`DATABASE_URL=postgresql+psycopg://aisop:数据库密码@db:5432/aisop`；URL中的特殊密码字符必须编码，且与`POSTGRES_PASSWORD`一致。
3. 把站点证书与私钥放到`deploy/certs/fullchain.pem`和`deploy/certs/privkey.pem`。客户端必须信任该证书。
4. 构建并启动：

```sh
docker compose up --build -d
docker compose ps
docker compose logs --tail=100 api worker
```

通过`https://服务器域名`登录。仅Nginx发布80/443端口；API、数据库不向外发布。首次启动由`init`创建v1数据库表和运行指标表；本次初始化还会为已有v1表补建查询索引，不改变既有记录或字段。其他版本仍需要明确的数据库迁移。

镜像构建与PostgreSQL部署在本次Windows环境中未实际执行，不能把本机SQLite测试等同于PostgreSQL生产验收。部署后必须运行本节验收与第4节PostgreSQL专项测试。

## 2. Windows本机开发验证

已有`D:/Anaconda/python.exe`环境包含推理、FastAPI和测试依赖。其他环境请安装`requirements-api.txt`、`requirements-worker.txt`和`requirements-test.txt`，并单独安装与硬件匹配的PyTorch。开发时可显式设置SQLite；生产模式禁止使用SQLite、禁止关闭Secure Cookie。

在两个PowerShell终端中设置**相同**环境变量，密码自行设置，勿提交至代码库：

```powershell
$env:PYTHON = 'D:/Anaconda/python.exe'
$env:APP_ENV = 'development'
$env:DATABASE_URL = 'sqlite:///D:/Project/myarc/.runtime/local.db'
$env:DATA_DIR = 'D:/Project/myarc/.runtime/local-data'
$env:MODEL_PATH = 'D:/Project/myarc/models/yolo11s-pose.pt'
$env:COOKIE_SECURE = 'false'
$env:APP_USERNAME = 'admin'
$env:APP_PASSWORD = '<自行替换为至少12位密码>'
```

初始化前创建`.runtime`目录：

```powershell
New-Item -ItemType Directory -Force .runtime
npm run server:init
npm start
```

另一个终端运行：

```powershell
npm run server:worker
```

浏览器打开`http://127.0.0.1:8000`。本机开发只监听回环地址；远程访问使用上述HTTPS部署，不将开发Cookie配置直接用于公网。仅启动API会让任务停留在等待状态，必须同时运行Worker。

原有静态演示仍可通过`npm run preview`运行在8080端口，与服务器模式分开。来源与授权说明见`docs/legacy-preview.md`。

## 3. 功能与限制

- 登录、退出、Cookie会话、写操作CSRF校验和任务所有者检查；首版是一个配置管理员账户，尚无用户管理和细分角色。
- 视频流式上传，完成解码校验后才登记READY媒体。支持MP4/WebM/MOV/AVI/M4V；最大500 MiB、10分钟、4K/120fps。首版分析要求近似恒定帧率；不规则时间戳明确失败。
- 创建任务必须携带Idempotency-Key；同键同内容返回原任务，同键异内容返回409。API不等待GPU完成。
- Worker独立领取任务，每次领取生成递增token，定期续租。崩溃后任务租约过期会被重领，最多3次；旧token不能提交结果。
- 取消采用QUEUED→CANCELLED或RUNNING→CANCELLING→CANCELLED。不能立即抢占GPU内核，通常在心跳检测后的帧处理检查点退出。
- 仅SUCCEEDED任务开放产物。Worker按`RUN_RETENTION_SECONDS`、`UPLOAD_RETENTION_SECONDS`和`CLEANUP_INTERVAL_SECONDS`自动清理过期终态任务、孤儿上传和`.part`文件；执行前可用`npm run server:cleanup -- --dry-run`审计范围。
- 结果包含框、骨架、姿态片段、证据图、报告、CSV、JSON及哈希清单；SOP始终为REVIEW，指标不伪造合格率。
- WebM预览最大1280×720、15fps，不保留音频。源视频完整保留在服务端；采样间标签保持只用于回放，不证明动作全程持续。
- 原图不全量缓存，证据图最多6张；结构化观测仍保存在内存，明确限制6000采样帧、50000个人员观测。超限任务失败，后续再扩展分块结果和分页骨架。
- 首版Worker串行执行上传任务，同进程缓存一个模型；实时API使用有界模型池。无训练服务、生产放行或高可用承诺。
- `/api/v1/metrics`提供队列等待、处理耗时、推理P95、显存峰值、吞吐和失败原因；JSON/报告中的processingSeconds包含分析与证据阶段，仍不包含任务排队、Worker首次加载模型及最终数据库提交。

## 4. 测试

```powershell
npm run test:api
```

单元与API测试使用独立临时数据库和合成视频，覆盖鉴权、CSRF、非法上传、幂等、任务隔离、取消、租约恢复、旧token拒绝、Range下载、路径穿越防护和空检测。编排测试注入的假分析器仅用于测试，不存在于API的生产选项中。

```powershell
npm run test:e2e
```

端到端测试自动启动独立API与Worker，使用临时密码和数据库，从已有车间视频生成短片，执行**真实GPU推理**；验证登录、上传、回放、CSV、报告、刷新恢复、任务历史及移动端布局。需要本机Python推理环境、模型和Edge/Playwright。可用`PYTHON`、`PLAYWRIGHT_MODULE`、`BROWSER_CHANNEL`指定运行环境。日志、截图与结果保存在`.runtime/e2e-*`，测试结束关闭自己启动的服务。

运行 `npm test`（或 `npm run test:ui`）自动启动随机本机端口上的独立预览服务，依次检查静态界面、分析样例与模拟API异常交互，并在结束后关闭服务。截图保存在 `.runtime/ui-*`，不会覆盖原参考素材。仅运行异常交互回归可用 `node tools/preview-test.cjs ui-regression.cjs`。

PostgreSQL专项测试需要独立测试库和psycopg驱动：

```powershell
$env:TEST_POSTGRES_URL = 'postgresql+psycopg://测试用户:密码@127.0.0.1:5432/测试库'
npm run test:api
```

专项测试创建随机schema，验证并发领取只有一个成功，完成后删除该测试schema。不要设置为生产数据库。未设置时明确跳过该项。

## 5. 重启、备份与故障排查

- 等待任务不执行：检查Worker进程、模型路径和Worker数据库是否与API一致。
- 模型变更：任务冻结模型文件哈希。排队后权重改变会报MODEL_CHANGED，请创建新任务，不覆盖旧产物。
- 分析失败：按任务ID查看Worker日志，界面只显示结构化原因，不暴露服务器路径和堆栈。
- 关闭页面：任务继续执行；再次登录后从任务历史进入。正在上传且尚未创建的任务不属于这一保证。
- 服务重启：会话、任务、媒体和结果由持久数据库与卷保存。RUNNING任务在租约过期后重试，不采用失效Worker未发布的目录。
- 备份：一致备份PostgreSQL和analysis-data卷；仅有数据库备份不能恢复媒体。保留模型版本及配置，定期演练恢复。
- 清理：不要执行`docker compose down -v`，它会删除命名卷。普通停止使用`docker compose stop`。
- 密码轮换：更改环境变量并重启API；同时按运维流程撤销旧会话，首版没有账户管理页面。

## 6. 接下来的开发范围

首先完成目标服务器上的PostgreSQL、容器、HTTPS、模型池容量和完整长度视频验收，再实施多用户权限、结果分块与媒体配额。现场准确率以`docs/accuracy-evaluation.md`定义的独立标注集和遮挡/光照/工位分组指标为准；实时相机、工具／孔位模型、PLC/MES、正式SOP规则按研究文档后续阶段推进。
