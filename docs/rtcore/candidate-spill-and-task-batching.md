# RTcore Candidate Spill 与硬件任务合批设计

## 目标与实现范围

candidate spill/resume 允许 shader callback 执行期间释放物理遍历资源；硬件任务合批把已经等待的稀疏后续 ray 请求装入同一组 execution lane，提高 traversal 并行度。两者共享同一原则：parent 的 RT Local SRAM 和 PDS 不迁移，执行代理只改变访问索引。

spill使用固定parent-lane slot；任务合批处于traversal边界，不支持运行中插入新lane，也尚未按node或AABB key排序。

## Parent 与 execution lane

parent 是 shader 的原始物理warp行和lane；它拥有ray header、candidate/committed hit records、callback decision及PDS。execution lane 是当前RTcore session中的遍历位置，拥有本轮临时ray context、stack与IU状态。一个parent可以先后使用不同execution lane，但持久数据仍在同一parent位置。

合批仅改变 `execution lane → parent warp/lane` 映射。scheduler通过locator和member table解析parent，所有持久SRAM读写都使用解析后的row/lane。shader收到结果后继续在原warp、原lane执行，寄存器和PDS不交换。

~~~text
原 shader warp/lane
    │ 写 ray header / callback decision，发 traverse
    ▼
parent 请求登记 → 空闲边界合批 → execution lane 遍历
    ▲                                  │
    │ 原 warp 的返回状态                │ hit record / spill pointer
    └──────── 完成回包解映射 ────────────┘
                         写入原 parent SRAM
~~~

## 软件与硬件分工

| 责任 | 软件或driver | 硬件 |
| --- | --- | --- |
| ray与shader控制流 | lowering写header、发traverse、执行callback并决定后续shader | 执行DFS及固定求交动作 |
| callback选择 | 写ACCEPT、IGNORE或TERMINATE，必要时提交candidate到committed plane | 读取decision，恢复或消费continuation |
| spill内存 | driver分配隐藏device-memory pool，提供布局与epoch | 按parent定位固定地址，执行LSU传输 |
| 合批 | 不增加软件排序、LDS收集或全组barrier | 登记请求、生成代理locator、完成后分别回包 |
| shader组织 | 保持原warp/lane及PDS | 不迁移shader寄存器，不重新组织着色warp |

traverse仍是共同入口，不需要新增shader resume指令。硬件根据parent的continuation-valid区分新遍历与spill恢复，callback decision决定恢复动作。SRAM中的非零pointer不是resume标志。

## 固定 Global Spill Slot

### 分配与地址

driver的 `RtSpillPool` 为每个SM分配256个slot，每slot384B。因此每SM固定预留98,304B，即96KiB；这是预留容量，不是每轮都会写入96KiB。只有产生candidate的lane写自己的slot。

dispatch descriptor包含 `spillPoolBase`、`slotsPerCore`、`perCoreStride`、`dispatchEpoch`。当前每core stride为96KiB；scheduler使用自己的core/SM ID定位分区：

~~~text
slotIndex    = parentWarp * 32 + parentLane
partition    = spillPoolBase + coreId * perCoreStride
spillBasePtr = partition + slotIndex * 384
~~~

布局由driver约定，硬件只做地址物化，不运行allocator。硬件在初始化新parent遍历时，将该固定地址高word、低word依次装入parent RT Local reserved17/16。实际spill从该parent的pointer读取地址并发起LSU传输。

pool不属于PDS，不作为shader可用的allocation发布。driver首次按core数量分配，后续dispatch复用allocation并更新非零epoch，device关闭时释放。当前没有PTE、freshCursor、recycle ring或跨SM atomic/CAS。

同parent在相同深度反复candidate→resume→candidate使用同一个slot，不重新找空闲块；execution lane变化不改变slot地址。恢复消费的是continuation有效性，不是释放给其他parent的地址。

### Slot 布局

| 相对地址 | 大小 | 内容 |
| --- | ---: | --- |
| +0x000 Page0 | 128B | 32个base stack nodeRef |
| +0x080 Page1 | 128B | base/overflow object-tag bitmap、总depth、image-valid、最后Instance nodeRef，其余word保留并编码为0 |
| +0x100 Page2 | 128B | 32个overflow nodeRef |

Page1的word0/1/2/3/4依次为base tags、overflow tags、stack depth、valid、Instance nodeRef。无Instance时用InvalidNodeRef。总可编码stack depth最多64；base部分32项，overflow部分32项。

物理session只有四份共享overflow stack，并非每个execution lane都有独立overflow。需要overflow的lane在资源可用时推进；global slot的Page2固定预留不增加物理overflow并行容量。

每次spill写全部三页，每页请求都等待对应LSU response后再继续。普通base-only恢复只读Page0/1；depth>32时再读Page2。TERMINATE不读取任何stack page。LSU地址要求128B对齐；当前共享页buffer不另外复制第二份完整image。

### Pointer 与有效性

| 状态 | 存放位置 | 意义 |
| --- | --- | --- |
| spillBasePtr | parent SRAM reserved16/17，64-bit | 固定slot地址，恢复后仍保留 |
| continuation-valid | `RtSpillBindings` 的256个bit，按parent索引 | 是否有尚未消费的continuation |
| image-valid | global slot Page1 word3 | 正常恢复时检查image完整性的内容标记 |
| callback decision | parent RT Local control word | shader选择的ACCEPT/IGNORE/TERMINATE |

continuation-valid共32B硬件状态，跨物理session释放保留；epoch改变时全部清零。Page1可能留下旧image，pointer也可能非零，但valid=0时不能把它们当作待恢复continuation。

## Candidate 发布与物理资源释放

opaque命中更新committed plane；non-opaque triangle或procedural AABB产生candidate plane。两份记录分开存储，因此“先遇到opaque，再遇到candidate”不会用后者覆盖已有committed hit。

发布candidate completion的顺序是：

1. traversal所有活跃lane达到本轮terminal状态，pending IU结果已由lane FSM消费。
2. LocalState flush candidate及committed hit-record写入原parent SRAM，并收到drain确认。
3. 等待continuation控制空闲、AS table和Local narrow read路径quiescent，进入spill阶段；spill LSU独占传输时不与AS请求交错。
4. 对candidate lane读原parent的固定pointer、取得stack snapshot，依次写Page0、Page1、Page2，并等每页ack。
5. 三页全部完成后，置该parent continuation-valid=1。它才是continuation发布点，不是首次安装非零pointer。
6. 全部candidate spill完成后，清理物理stack/session及私有LocalState ownership，再发布 `contextReleased=true` 的completion。

这里“清理LocalState”指硬件私有gather/flush状态，不是清除持久parent SRAM。parent header、两份hit记录、callback数据和PDS仍然保留。AS waiter必须drain，但物理retire不清空已经返回的AS cache line。

shader必须等自己的traverse completion/writeback后才能进入callback，因此不会在spill未完成时resume。把LSU请求送出或Page1写出都不足以发布completion；还必须等待Page2 ack和物理reclaim条件。

## Callback 与恢复

candidate callback仍在原parent shader lane执行。ACCEPT及TERMINATE的candidate提交由软件完成：复制需要的candidate记录和属性到committed plane。硬件不使用旧的私有candidate覆盖shader修改过的committed记录。IGNORE保留先前的committed hit。

软件对仍需消费continuation的lane再次发traverse，包括TERMINATE lane。resume不重写fresh-ray header，也不能因TERMINATE就直接从请求mask删除该lane。

| decision | Stack读取 | 硬件动作 | 后续软件动作 |
| --- | --- | --- | --- |
| IGNORE | Page0/1，必要时Page2 | 保留committed，恢复DFS，清continuation-valid | 继续处理后续candidate或最终hit/miss |
| ACCEPT | Page0/1，必要时Page2 | 从parent重读shader提交的committed/HitT，恢复stack并用有效上界继续DFS，清valid | 按最终返回状态选择shader |
| TERMINATE | 不读stack page | 消费并清valid，返回TERMINATED | 按已提交hit和skip-closest-hit等规则继续shader |

恢复时，bindings先查询解析后的parent valid；valid=1才读取pointer/decision并走restore。非TERMINATE恢复将stack安装进当前execution lane后消费valid；TERMINATE的transfer直接完成并消费valid。固定pointer都不清零，也没有slot入ring动作。

slot中的旧image无需当场擦除；逻辑有效性由bindings控制。新一轮candidate再次完整覆盖同一slot，然后重新发布valid。重复消费或重复发布由bindings断言检查。

### 不 Spill 的状态与重建

slot不保存prepared ray、tmax、完整instance metadata、candidate/committed hit record、PDS payload或AS cache line。stack保存world/object tag和Instance nodeRef，而不是完整变换结果。

恢复包含object-tagged entry时，scheduler用保存的Instance ref重新走Instance读取与transform，并通过映射后的parent SRAM获取world ray、HitT和AS base。world-tagged triangle所需的prepared ray也从当前parent的world状态重新执行RaySetup，不复用旧execution lane的准备结果。

### Disabled 与资源不足

启用自动spill要求pool base、epoch非零，且slotsPerCore至少256。driver分配失败时dispatch启动返回错误，不发布缺少内存的任务。当前固定256个parent slot不存在运行时“申请第257个slot”的free-list耗尽过程。

没有有效pool的legacy路径保留物理candidate context，reservation仍独占，需要按旧流程resume/release；它不进入多parent合批路径。这个fallback不应描述成当前实现具有动态pool-empty allocator或recycle-ring机制。

## 后续 Ray 识别

`RtParentRayHistory` 每个物理warp维护两份32-bit mask：`seen`和`secondary`，8个warp合计64B。它观察实际被SRAM接受的软件SlotStatus=TRACE_REQUEST写入：

- 第一次发布该lane的新ray，seen由0变1，secondary保持0。
- 已seen的lane再发布新ray，secondary置1。
- candidate resume不重新发布header，因此保留当前ray的分类。
- 新warp分配到复用的物理row时清零两份mask。

这里的secondary是“同lane首次ray之后的新ray”，不是几何类型识别器。递归深度不能代替这个判断：reflections在raygen循环内反复TraceRay，后续反射ray的active level仍是0。

一个请求只有所有有效lane都标记secondary、且mask不是全32-bit时，才可以参与合批。含首次ray的混合请求保持独立；满warp请求也不与其他请求合并。初次primary的candidate resume保持primary分类，后续ray的candidate resume可进入合批。

## 请求接纳与合批

### Per Parent 接纳额度

`RtSessionReservation(multiWarp=true)` 为每个parent记录traversing和provisional状态：同parent不能同时登记第二条traverse，不同parent在有效driver pool下可以并发进入operand collection。

接纳发生在有效SIMT mask确定前，因此先占provisional额度。有效mask为空时取消自己的额度，不干扰其他parent；非空时额度保持至对应completion。legacy或ISA groupTraverse仍要求exclusive ownership，必要时先drain其他parent；软件release ack才能释放retained legacy ownership。

### 请求表与选择规则

`RtTaskBatcher` 每parent一项，最多8项，保存spill-pool descriptor、active mask、issue ID、目的寄存器和eligible标志；warp ID由表项索引隐含，不保存ray参数。

RTcore idle时，round-robin选择一个pending parent，从该位置环绕扫描其余请求。其他请求必须也eligible、descriptor完全相同，并且各bank累加的活跃lane数都不超过4。符合条件才加入整个parent请求。

初次ray、满warp等不可合并请求在idle且无pending时直接透传；忙时可登记为等待项，但之后仍独立执行。无有效pool和外部ISA groupTraverse沿用legacy直接路径，不参与内部合批。

没有等待“所有warp READY”的软件循环，没有为收集请求增加全组barrier，也不为填满lane设置人工等待窗口。跨workgroup合批只需这些请求驻留于同一SM且descriptor兼容；不是shader跨workgroup同步。

### Bank 保持与代理映射

每bank对应四个execution位置：

~~~text
bank b:  b, b+8, b+16, b+24
parentLane % 8 必须等于 executionLane % 8
~~~

对每bank，按已选择parent顺序及其原column顺序压紧lane。例子：

| 原 parent | bank | 分配的execution lane | 回包目标 |
| --- | ---: | ---: | --- |
| warp A lane0 | 0 | 0 | warp A lane0 |
| warp A lane8 | 0 | 8 | warp A lane8 |
| warp B lane24 | 0 | 16 | warp B lane24 |
| warp B lane1 | 1 | 1 | warp B lane1 |

warp B lane24虽然在execution lane16遍历，header/hit/spill仍访问B lane24。当前映射保存slotOwner和slotColumn，bank由execution lane隐含，总计32×(3+2)=160bit，即20B。

内部合批复用scheduler的grouped locator路径，并生成直接映射物理parent row的member table；这个内部grouped bit不意味着shader执行了ISA group collective，不改变pipeline原本的group完成分类。

由于whole-parent fit约束，如果某请求的bank0装不下，即使其他bank空闲也不能拆开加入。这个策略限制填充率，但降低数据路由与完成协议复杂度。

### 完成回包与 Backpressure

batcher状态机是idle→send→waitResult→idle，直接请求使用direct状态。send阶段固定built command和映射，scheduler未接受时保持不变。

waitResult阶段持有scheduler已有的completion，不额外复制32-lane状态快照。按remaining parent逐个恢复原warp ID、issue ID、目的寄存器、completion mask和lane status。只有最后一个parent结果被接受后，才向scheduler确认整份completion并回idle。

一个原parent lane的状态只可能来自同bank的四个execution slot。例如A lane16的状态可来自0/8/16/24，实际来源由slotOwner/slotColumn确定。这里选择的是32-bit返回状态；hit record和spill pointer已经直接写入parent SRAM，不需要再从代理搬回。

回包受阻时保持remaining及输出稳定。每个parent的completion分别进入原warp的writeback/scoreboard流程，不能只解除batch首个warp的额度，也不能把内部代理group当作shader collective。

批次必须使用自动spill并最终contextReleased=true，才能释放执行资源后服务其他parent。全部parent结果返回前，本实现不会启动下一物理batch；已返回parent的后续请求可在自己的额度释放后先登记。

## 正确性不变量与验证

1. 一个parent最多一条未完成traverse；任务表、remaining与接纳额度不能混淆。
2. execution lane唯一映射到一个parent；持久读写和slot地址不能使用代理lane替代parent。
3. candidate及committed planes同时保留；shader提交的committed不能被旧物理状态覆盖。
4. hit-record drain、所有spill页ack、物理reclaim都在callback可执行的completion之前。
5. resume只消费一次continuation-valid；固定pointer和历史image不代表有效resume。
6. 每个原请求返回一次completion；请求lane数等于回包lane数及物理命令histogram的加权lane数。
7. pool epoch不能在同RTcore并发复用；不同descriptor不合批，但这不提供并发epoch的隔离能力。

定向测试覆盖slot编码、fixed bindings、parent SRAM访问、spill全页ack、base-only与TERMINATE读取规则、object恢复、candidate/committed生命周期、独立parent额度、bank容量不足不拆请求、请求与回包backpressure、新ray历史和warp row复用。

构建和图像回归入口见[验证说明](validation.md)。具体提交、运行库哈希、图像差异和性能测量保存在 `artifacts/rtcore-regression/`，不作为本设计文档的固定通过结论。

## 代码入口与后续演进

| 内容 | 源码 |
| --- | --- |
| driver隐藏pool与固定布局 | [rt_spill_pool.hpp](../../driver/common/rt_spill_pool.hpp) |
| dispatch descriptor填入 | [RTL driver ventus.cpp](../../driver/driver/rtlsim_device/ventus.cpp) |
| slot codec、bindings、parent access、LSU transfer | [RtSpill.scala](../../gpgpu/ventus/src/rtcore/RtSpill.scala) |
| spill/restore与completion/reclaim时序 | [RtSchedulerLifecycle.scala](../../gpgpu/ventus/src/rtcore/RtSchedulerLifecycle.scala) |
| parent额度、合批、ray历史 | [RtSessionReservation.scala](../../gpgpu/ventus/src/rtcore/RtSessionReservation.scala) |
| 接纳、header发布观察、scoreboard | [pipe.scala](../../gpgpu/ventus/src/pipeline/pipe.scala) |
| batcher与scheduler连接 | [RtCore.scala](../../gpgpu/ventus/src/rtcore/RtCore.scala) |
| fresh/resume lowering与callback提交 | [vt_nir_build_traversal.c](../../mesa/src/ventus/compiler/vt_nir_build_traversal.c)、[vt_nir_lower_rt_io.c](../../mesa/src/ventus/compiler/vt_nir_lower_rt_io.c) |

node相干优先级、跨bank自由打包、按空闲slot动态补位均未实现。运行中补位需要新增按slot生命周期的安全检查、局部gather/stack清理、in-flight IU/AS请求归属和逐parent完成跟踪，不能只放宽command.ready。未来演进应继续保持parent数据不迁移，并单独验证路由成本、吞吐、shader重叠及图像正确性。
