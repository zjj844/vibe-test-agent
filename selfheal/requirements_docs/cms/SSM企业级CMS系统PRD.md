SSM 企业级 CMS 内容管理系统 验收需求（FR）
验收口径：每条 FR 均为可测试断言，「必须」「不得」为硬性要求；code 指统一返回体中的业务码，不是 HTTP 状态码。接口统一前缀 /api，一律返回 {code, msg, data}。全部条目通过方可交付。
FR-1 登录返回 token 与权限码
POST /api/auth/login 成功时 code=200，data 必须包含 token 与该用户的完整权限码集合；admin 必须返回 11 个权限码。
FR-2 注册默认 viewer
POST /api/auth/register 无需登录即可调用，新用户必须默认绑定 viewer 角色，不得默认获得任何管理权限码。
FR-3 错误密码不得成功
密码错误时 code 不得为 200，msg 必须提示用户名或密码错误，且不得返回 token。
FR-4 未登录返回 401
未携带 token 或 token 已失效时，受保护接口必须返回 code=401，msg 为「未登录或登录已过期」，不得返回 500，不得返回 200。
FR-5 退出后旧 token 立即失效
POST /api/auth/logout 后，原 token 再请求受保护接口必须返回 code=401，不得继续有效到自然过期。
FR-6 禁用用户立即失访
用户被 PUT /api/user/{id}/status 禁用后，其 token 必须立即无法访问受保护接口，不得等到 token 过期。
FR-7 会话身份不得串号
请求结束后必须清理 ThreadLocal；并发场景下后一个请求不得读到前一个请求的登录身份。
FR-8 密码不得明文
sys_user.password 必须存 SHA-256 加 8 位随机盐的 salt$hash，相同明文两次编码结果必须不同，库中不得出现明文密码。
FR-9 三类角色边界
admin 必须可访问全部管理接口；editor 必须无 article:audit；viewer 必须不绑定任何管理权限码，仅可查看分类树与文章列表、详情。
FR-10 权限码缺失返回 403
@RequirePermission 校验不通过时必须返回 code=403，msg 为「无权限访问该接口」，不得返回 200，不得进入业务方法。
FR-11 editor 不得审核
editor 调用 PUT /api/article/{id}/audit 必须返回 code=403，文章 status 不得发生变化。
FR-12 viewer 不得管理
viewer 调用任意管理接口（用户、分类写操作、文章写操作、审核、日志）必须返回 code=403。
FR-13 不得修改他人文章
editor 修改或删除非本人署名的文章必须返回 code=403，msg 为「只能修改自己的文章」；admin 不受此限。
FR-14 鉴权不得写进业务方法
18 个接口的权限校验必须全部由 AOP 切面完成，Controller 与 Service 内不得出现权限码 if 判断；新增接口只加一行 @RequirePermission 即可生效。
FR-15 统一返回体
所有接口成功与异常路径必须都返回 {code, msg, data} 三段式，前端只需判断 code，不得为异常单独写解析分支。
FR-16 空集合不得为 null
分页接口 data.records 为空时必须返回 []，不得为 null；分类树 children 为空时必须返回 []，不得为 null。
FR-17 参数校验失败返回 400
JSR303 校验失败（如 title 为空字符串或纯空格）必须返回 code=400，msg 为「文章标题不能为空」，不得返回 500。
FR-18 资源不存在返回 404
查询不存在的文章或分类必须返回 code=404，msg 为「文章不存在」／「分类不存在」，不得返回 500，不得返回 null。
FR-19 系统异常不得暴露堆栈
未捕获异常必须返回 code=500 的统一 JSON，data 中不得包含异常堆栈、SQL 语句或类名。
FR-20 非法审核状态拒绝
审核时 status 传 9 等非合法值必须返回非 200，msg 为「审核状态不合法」，且文章 status 不得改变。
FR-21 用户分页仅 admin
GET /api/user/page 仅 admin 可用，editor 与 viewer 必须返回 code=403；admin 返回的 data 必须含 total 与 records。
FR-22 分配角色即时生效
PUT /api/user/{id}/role 成功后，该用户后续请求必须按新角色的权限码集合鉴权，不得沿用旧集合。
FR-23 分类树层级正确
GET /api/category/tree 对三级分类必须返回三层嵌套 JSON，同级按 sort 升序、id 升序，不得扁平返回。
FR-24 父分类不得设为自己
编辑分类时 parent_id 等于自身 id 必须被拒绝，不得形成自环，不得导致建树无限递归。
FR-25 有子分类不得删除
DELETE /api/category/{id} 在该分类存在子分类时必须返回非 200，msg 含「存在子分类」，且不得产生孤儿节点。
FR-26 有文章不得删除
DELETE /api/category/{id} 在该分类下存在文章时必须返回非 200，msg 含「存在文章」，文章列表的分类名不得出现空白。
FR-27 分类树一次查库
建树必须一次查全表后内存递归，不得按层级逐层查库，不得产生 N 次数据库往返。
FR-28 文章新增强制待审核
POST /api/article 必须忽略前端传入的 status，服务端强制置 1（待审核），view_count 置 0，作者取当前登录用户名。
FR-29 编辑已发布回退待审核
PUT /api/article/{id} 对 status=2 的文章必须把 status 回退为 1，不得保留已发布状态，防止「先过审再改文」。
FR-30 审核只接受三种目标状态
PUT /api/article/{id}/audit 仅接受 0（驳回为草稿）、2（发布）、3（下架）；通过必须为 2，驳回必须为 0，下架必须为 3。
FR-31 下架文章不得直接发布
已下架（3）文章不得经由审核接口直接变为已发布（2），只能由作者编辑后回退为 1 重新排队。
FR-32 审核最小更新
审核必须只更新 id 与 status，不得整行写回，不得覆盖并发下他人刚修改的标题或正文，不得误写 is_deleted。
FR-33 详情浏览量自增
GET /api/article/{id} 每成功调用一次，view_count 必须 +1；文章不存在时不得自增。
FR-34 删除为逻辑删除
DELETE /api/article/{id} 必须把 is_deleted 置 1，数据库行不得物理消失，所有查询必须自动追加 is_deleted = 0。
FR-35 已删除文章不得出现在列表
逻辑删除后的文章不得出现在 GET /api/article/page 的 records 中，也不得计入 total。
FR-36 组合检索同时生效
「标题模糊 + 分类 + 状态」任意组合查询时，返回结果必须同时满足全部已传条件，不得只按其中一个条件过滤。
FR-37 分页总数与页大小
pageNum=2&pageSize=5 且总数大于 5 时，records 必须为 5 条，total 必须为真实总数；缺省参数必须默认为 1 / 10，传 pageNum=0 不得抛异常。
FR-38 列表页固定 2 条 SQL
列表页必须固定为 2 条 SQL（COUNT + 数据查询），不得出现 N+1；一页 10 条时不得出现 11 条 SQL。
FR-39 分类名由 SQL 联表取得
列表中的 category_name 必须由 LEFT JOIN 一次查出，不得在 Service 中循环按 category_id 二次查询。
FR-40 分页 Mapper 约定
分页方法第一个参数必须是 Page，多参数必须用 @Param 命名，Mapper XML 中不得手写 LIMIT；分页插件必须注册 DbType.MYSQL。
FR-41 日志字段完整
GET /api/log/page 返回的记录必须含操作人、模块、操作内容、操作方法、IP、耗时（毫秒）与操作时间。
FR-42 写操作必须留痕
每一次写操作必须由 @LogRecord 切面自动落库，业务代码中不得出现手工 save 日志；操作抛异常时同样必须记录后再原样抛出。
FR-43 日志失败不影响主流程
日志写入异常不得导致业务接口失败，不得改变业务返回码，只可打印 warn 日志。
FR-44 首次启动即可演示
sys_user 为空时必须自动写入 3 个账号（admin / editor / viewer，密码均为 123456）、3 个角色、11 个权限码、6 个分类、4 篇示例文章，启动后不得为空库。
FR-45 时间字段自动填充
create_time 与 update_time 必须由 MetaObjectHandler 自动填充，业务代码不得手写，更新时 create_time 不得被改写。
FR-46 接口不得一律 404
DispatcherServlet 的 url-pattern 必须为 /，所有 18 个 /api 接口必须可正常映射，不得因 /* 导致全部返回 404；静态资源必须仍由容器处理。
FR-47 切面与 Controller 同容器
com.cms.aop 必须由 SpringMVC 子容器扫描；权限切面必须对 Controller 生效，不得出现「注解写了但从不校验」。父容器不得创建第二份 Controller Bean。
FR-48 验收用例全部通过
12 条功能用例（TC-01～TC-12）与 6 条异常／安全用例（TS-01～TS-06）必须全部通过，失败数为 0 方可交付。
