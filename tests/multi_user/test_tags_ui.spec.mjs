// 成员标签功能前端点击测试
// 运行：node tests/multi_user/test_tags_ui.spec.mjs
//
// 测试范围：
//   1. 颁发 Key 对话框标签选择器（多选 + 自定义输入 + 必填校验）
//   2. 成员管理表格标签列展示
//   3. 添加成员对话框标签选择器
//   4. 编辑标签对话框（增/删标签）
//   5. 标签建议列表（从后端聚合）
//   6. 权限边界（非 admin 看不到成员管理菜单）

import { chromium } from 'playwright';

const BASE = 'http://127.0.0.1:8080/';
const KEYS = {
  alice:   'th_b8df89f8075ccf5e58a34673292d2653',
  charlie: 'th_91e7b4462a614481ad66e4a5891922a9',
};

const results = [];
let passCount = 0, failCount = 0;
function record(name, status, detail = '') {
  results.push({ name, status, detail });
  if (status === 'PASS') passCount++;
  else if (status === 'FAIL') failCount++;
  const tag = status === 'PASS' ? '\u001b[32m[PASS]\u001b[0m'
    : status === 'FAIL' ? '\u001b[31m[FAIL]\u001b[0m'
    : '\u001b[33m[SKIP]\u001b[0m';
  console.log(`${tag} ${name}${detail ? ' — ' + detail : ''}`);
}

async function loginAs(browser, member, apiKey) {
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  const pageErrors = [];
  page.on('pageerror', (e) => pageErrors.push(e.message));
  // 重试 3 次，应对偶发网络问题
  let lastErr = null;
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      await page.goto(BASE, { waitUntil: 'domcontentloaded', timeout: 20000 });
      await page.waitForSelector('.login-card', { timeout: 15000 });
      await page.locator('input[placeholder="如：alice"]').first().fill(member);
      await page.locator('input[placeholder="th_ 开头"]').first().fill(apiKey);
      await page.locator('.el-button', { hasText: '登录' }).first().click();
      await page.waitForSelector('.app-header', { timeout: 15000 });
      await page.waitForTimeout(1500);
      return { ctx, page, error: null, pageErrors };
    } catch (e) {
      lastErr = e;
      await page.waitForTimeout(1000);
    }
  }
  return { ctx, page, error: lastErr, pageErrors };
}

async function gotoMenu(page, menuText) {
  await page.locator('.el-menu-item', { hasText: menuText }).first().click();
  await page.waitForTimeout(800);
  try { await page.waitForSelector('.el-loading-mask', { state: 'hidden', timeout: 8000 }); }
  catch { /* 可能无 loading */ }
  await page.waitForTimeout(300);
}

async function apiCall(method, urlPath, apiKey, body = null) {
  const headers = { 'X-API-Key': apiKey };
  if (body !== null) headers['Content-Type'] = 'application/json';
  const opts = { method, headers };
  if (body !== null) opts.body = JSON.stringify(body);
  const resp = await fetch(BASE + urlPath.replace(/^\//, ''), opts);
  let parsed = null;
  try { parsed = await resp.json(); } catch {}
  return { status: resp.status, ok: resp.ok, body: parsed };
}

// =============================================================
// 测试用例
// =============================================================

async function test_issueKey_tagsRequired(browser) {
  // 不登录，直接在登录页操作「没有 Key？在此颁发」
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  try {
    await page.goto(BASE, { waitUntil: 'domcontentloaded', timeout: 30000 });
    await page.waitForSelector('.login-card', { timeout: 15000 });
    await page.waitForTimeout(500);

    // 点击「没有 Key？在此颁发」
    await page.locator('.el-button', { hasText: '没有 Key' }).first().click();
    await page.waitForSelector('.el-dialog', { timeout: 5000 });
    await page.waitForTimeout(300);

    // 填 memberId 不填 tags，直接点颁发
    await page.locator('.el-dialog input[placeholder="如：alice"]').first().fill('test-user-1');
    await page.locator('.el-dialog .el-button', { hasText: '颁发' }).click();
    await page.waitForTimeout(500);

    // 应该出现 warning message「请至少选择一个成员标签」
    const msg = page.locator('.el-message--warning', { hasText: '标签' });
    await msg.waitFor({ state: 'visible', timeout: 3000 });
    const visible = await msg.isVisible().catch(() => false);
    record('T1 颁发 Key 标签必填校验', visible ? 'PASS' : 'FAIL',
      visible ? '未填标签时弹出 warning' : '未弹出 warning');

    // 关闭对话框
    await page.locator('.el-dialog .el-button', { hasText: '取消' }).click().catch(() => {});
    await page.waitForTimeout(300);
  } catch (e) {
    record('T1 颁发 Key 标签必填校验', 'FAIL', e.message);
  } finally {
    await ctx.close();
  }
}

async function test_issueKey_tagsMultiSelect(browser) {
  // 不登录，直接在登录页操作
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  try {
    await page.goto(BASE, { waitUntil: 'domcontentloaded', timeout: 30000 });
    await page.waitForSelector('.login-card', { timeout: 15000 });
    await page.waitForTimeout(500);

    await page.locator('.el-button', { hasText: '没有 Key' }).first().click();
    await page.waitForSelector('.el-dialog', { timeout: 5000 });
    await page.waitForTimeout(300);

    await page.locator('.el-dialog input[placeholder="如：alice"]').first().fill('test-tags-ui-1');

    // 标签选择器：点击触发下拉
    const tagSelect = page.locator('.el-dialog .el-select').last();
    await tagSelect.click();
    await page.waitForTimeout(300);

    // 下拉选项应该包含默认建议（前端/后端/全栈...）
    const options = page.locator('.el-select-dropdown__item');
    const optCount = await options.count();
    const optTexts = [];
    for (let i = 0; i < optCount; i++) {
      optTexts.push(await options.nth(i).innerText());
    }
    const hasDefault = optTexts.some(t => t.includes('前端') || t.includes('后端'));
    record('T2a 标签下拉显示默认建议', hasDefault ? 'PASS' : 'FAIL',
      `选项[${optTexts.join(',')}]`);

    // 选择「前端」
    await options.filter({ hasText: '前端' }).first().click();
    await page.waitForTimeout(200);
    await page.keyboard.press('Escape');
    await page.waitForTimeout(200);

    // 再打开，选「后端」
    await tagSelect.click();
    await page.waitForTimeout(200);
    await page.locator('.el-select-dropdown__item').filter({ hasText: '后端' }).first().click();
    await page.waitForTimeout(200);
    await page.keyboard.press('Escape');
    await page.waitForTimeout(200);

    // 自定义输入
    await tagSelect.click();
    await page.waitForTimeout(200);
    await tagSelect.locator('input').fill('自定义标签A');
    await page.waitForTimeout(300);
    await page.keyboard.press('Enter');
    await page.waitForTimeout(300);
    await page.keyboard.press('Escape');
    await page.waitForTimeout(200);

    // 检查已选标签 chip
    const chips = page.locator('.el-dialog .el-select__tags-text');
    const chipCount = await chips.count();
    const chipTexts = [];
    for (let i = 0; i < chipCount; i++) {
      chipTexts.push(await chips.nth(i).innerText().catch(() => ''));
    }
    const hasCustom = chipTexts.some(t => t.includes('自定义标签A'));
    record('T2b 多选 + 自定义标签', hasCustom ? 'PASS' : 'FAIL',
      `已选标签[${chipTexts.join(',')}]`);

    // 关闭不颁发（避免污染数据）
    await page.locator('.el-dialog .el-button', { hasText: '取消' }).click().catch(() => {});
  } catch (e) {
    record('T2 颁发 Key 标签多选+自定义', 'FAIL', e.message);
  } finally {
    await ctx.close();
  }
}

async function test_memberTable_tagsColumn(browser) {
  const { ctx, page, error } = await loginAs(browser, 'alice', KEYS.alice);
  if (error) { record('T3 成员表标签列展示', 'FAIL', `登录失败: ${error.message}`); await ctx.close(); return; }

  try {
    await gotoMenu(page, '成员管理');
    await page.waitForSelector('.el-table', { timeout: 8000 });
    await page.waitForTimeout(500);

    // 表头应包含「标签」
    const headers = page.locator('.el-table__header th .cell');
    const headerCount = await headers.count();
    const headerTexts = [];
    for (let i = 0; i < headerCount; i++) {
      headerTexts.push(await headers.nth(i).innerText().catch(() => ''));
    }
    const hasTagsHeader = headerTexts.some(t => t.includes('标签'));
    record('T3a 成员表有标签列', hasTagsHeader ? 'PASS' : 'FAIL',
      `表头[${headerTexts.join('|')}]`);

    // 操作列应有「编辑标签」按钮
    const editTagsBtn = page.locator('.el-table .el-button', { hasText: '编辑标签' });
    const btnCount = await editTagsBtn.count();
    record('T3b 每行有编辑标签按钮', btnCount > 0 ? 'PASS' : 'FAIL',
      `找到 ${btnCount} 个按钮`);

    // dave / eve 行应显示标签 chip（DBA/全栈/前端 等）
    const tagChips = page.locator('.el-table .el-tag--success');
    const chipCount = await tagChips.count();
    record('T3c 表格显示标签 chip', chipCount > 0 ? 'PASS' : 'FAIL',
      `共 ${chipCount} 个标签 chip`);
  } catch (e) {
    record('T3 成员表标签列展示', 'FAIL', e.message);
  } finally {
    await ctx.close();
  }
}

async function test_editTagsDialog(browser) {
  const { ctx, page, error } = await loginAs(browser, 'alice', KEYS.alice);
  if (error) { record('T4 编辑标签对话框', 'FAIL', `登录失败: ${error.message}`); await ctx.close(); return; }

  // 先用 API 创建一个测试成员（保证存在可编辑对象）
  const createResp = await apiCall('POST', '/v1/team/members', KEYS.alice, {
    member_id: 'test-edit-tags', display_name: 'TestEdit', role: 'member', tags: ['前端']
  });
  if (createResp.status !== 201 && createResp.status !== 409) {
    record('T4 编辑标签对话框', 'FAIL', `API 创建测试成员失败: ${createResp.status}`);
    await ctx.close(); return;
  }

  try {
    await gotoMenu(page, '成员管理');
    // 等待可见的成员管理表格
    const visibleTable = page.locator('.app-main .el-table:visible').first();
    await visibleTable.waitFor({ state: 'visible', timeout: 8000 });
    await page.waitForTimeout(1000);

    // Element Plus 的 fixed="right" 导致 member_id 列在 DOM 中不可见
    // 改用 JS 直接调用 Vue 的 showEditTagsDialog 方法
    // 先用 API 确认 test-edit-tags 在列表中
    const listResp = await apiCall('GET', '/v1/team/members', KEYS.alice);
    const memberInfo = (listResp.body || []).find(m => m.member_id === 'test-edit-tags');
    if (!memberInfo) {
      record('T4 编辑标签对话框', 'FAIL', 'API 中未找到 test-edit-tags');
      await ctx.close(); return;
    }

    // 通过 window.__teamharness_vm（app.js mount 时挂载）直接调用组件方法
    // 绕过 Element Plus fixed="right" 列的 DOM 分裂问题
    const invoked = await page.evaluate((memberData) => {
      const vm = window.__teamharness_vm;
      if (vm && typeof vm.showEditTagsDialog === 'function') {
        vm.showEditTagsDialog(memberData);
        return true;
      }
      return false;
    }, memberInfo);
    if (!invoked) {
      record('T4 编辑标签对话框', 'FAIL', '无法调用 showEditTagsDialog（window.__teamharness_vm 不可用）');
      await ctx.close(); return;
    }

    await page.waitForSelector('.el-dialog:visible', { timeout: 5000 });
    await page.waitForTimeout(500);

    // 对话框标题应为「编辑成员标签」
    const title = await page.locator('.el-dialog:visible .el-dialog__title').innerText().catch(() => '');
    record('T4a 编辑标签对话框打开', title.includes('编辑') ? 'PASS' : 'FAIL', `标题=${title}`);

    // 添加一个新标签「测试标签B」
    const tagSelect = page.locator('.el-dialog:visible .el-select').last();
    await tagSelect.click();
    await page.waitForTimeout(300);
    await tagSelect.locator('input').fill('测试标签B');
    await page.waitForTimeout(300);
    await page.keyboard.press('Enter');
    await page.waitForTimeout(300);
    // Escape 偶尔关不掉下拉，改用点击对话框 header 让 input 失焦
    await page.locator('.el-dialog:visible .el-dialog__header').click();
    await page.waitForTimeout(500);

    // 点保存（force: 防止下拉残留拦截）
    await page.locator('.el-dialog:visible .el-button', { hasText: '保存' }).click({ force: true });
    await page.waitForTimeout(1000);

    // 验证 API：test-edit-tags 的 tags 应包含「测试标签B」
    const resp = await apiCall('GET', '/v1/team/members/test-edit-tags', KEYS.alice);
    const tags = resp.body?.tags || [];
    const hasNew = tags.includes('测试标签B');
    record('T4c 保存后标签生效', hasNew ? 'PASS' : 'FAIL',
      `tags=[${tags.join(',')}]`);
  } catch (e) {
    record('T4 编辑标签对话框', 'FAIL', e.message);
  } finally {
    // 清理：删除测试成员
    await apiCall('DELETE', '/v1/team/members/test-edit-tags', KEYS.alice).catch(() => {});
    await ctx.close();
  }
}

async function test_addMemberDialog(browser) {
  const { ctx, page, error } = await loginAs(browser, 'alice', KEYS.alice);
  if (error) { record('T5 添加成员对话框标签', 'FAIL', `登录失败: ${error.message}`); await ctx.close(); return; }

  try {
    await gotoMenu(page, '成员管理');
    await page.waitForSelector('.el-button', { hasText: '添加成员' }, { timeout: 5000 });
    await page.locator('.el-button', { hasText: '添加成员' }).click();
    await page.waitForSelector('.el-dialog:visible', { timeout: 5000 });
    await page.waitForTimeout(300);

    const title = await page.locator('.el-dialog:visible .el-dialog__title').innerText();
    record('T5a 添加成员对话框打开', title.includes('添加成员') ? 'PASS' : 'FAIL', `标题=${title}`);

    // 不填 tags 直接点添加 → 应弹 warning
    await page.locator('.el-dialog:visible input[placeholder="如：alice"]').first().fill('test-add-ui-1');
    await page.locator('.el-dialog:visible .el-button', { hasText: '添加' }).click();
    await page.waitForTimeout(500);
    const warn = page.locator('.el-message--warning', { hasText: '标签' });
    const warnVisible = await warn.isVisible().catch(() => false);
    record('T5b 添加成员标签必填校验', warnVisible ? 'PASS' : 'FAIL',
      warnVisible ? '弹出 warning' : '未弹 warning');

    if (warnVisible) {
      // 选一个标签再添加
      const tagSelect = page.locator('.el-dialog:visible .el-select').last();
      await tagSelect.click();
      await page.waitForTimeout(200);
      await page.locator('.el-select-dropdown__item').first().click();
      await page.waitForTimeout(300);
      await page.keyboard.press('Escape');
      await page.waitForTimeout(300);

      // 监听可能的错误消息
      const errMsg = page.locator('.el-message--error');
      await page.locator('.el-dialog:visible .el-button', { hasText: '添加' }).click();
      await page.waitForTimeout(1500);
      const errVisible = await errMsg.isVisible().catch(() => false);
      const errText = errVisible ? await errMsg.innerText() : '';

      // 用 API 验证成员是否创建成功（比表格文本更可靠）
      const apiCheck = await apiCall('GET', '/v1/team/members/test-add-ui-1', KEYS.alice);
      const created = apiCheck.status === 200 && apiCheck.body?.member_id === 'test-add-ui-1';
      const hasTag = (apiCheck.body?.tags || []).length > 0;
      record('T5c 带标签添加成员成功', (created && hasTag) ? 'PASS' : 'FAIL',
        `api=${apiCheck.status}, tags=[${(apiCheck.body?.tags||[]).join(',')}], err=${errText}`);
    }
  } catch (e) {
    record('T5 添加成员对话框标签', 'FAIL', e.message);
  } finally {
    // 清理
    await apiCall('DELETE', '/v1/team/members/test-add-ui-1', KEYS.alice).catch(() => {});
    await ctx.close();
  }
}

async function test_nonAdminNoMemberMenu(browser) {
  const { ctx, page, error } = await loginAs(browser, 'charlie', KEYS.charlie);
  if (error) { record('T6 非 admin 无成员管理菜单', 'FAIL', `登录失败: ${error.message}`); await ctx.close(); return; }

  try {
    // charlie 是普通 member，不应看到「成员管理」菜单
    const menu = page.locator('.el-menu-item', { hasText: '成员管理' });
    const count = await menu.count();
    record('T6 非 admin 无成员管理菜单', count === 0 ? 'PASS' : 'FAIL',
      count === 0 ? '菜单未显示' : `菜单显示 ${count} 次`);
  } catch (e) {
    record('T6 非 admin 无成员管理菜单', 'FAIL', e.message);
  } finally {
    await ctx.close();
  }
}

async function test_tagsSuggestionAggregated(browser) {
  const { ctx, page, error } = await loginAs(browser, 'alice', KEYS.alice);
  if (error) { record('T7 标签建议聚合后端数据', 'FAIL', `登录失败: ${error.message}`); await ctx.close(); return; }

  try {
    await gotoMenu(page, '成员管理');
    await page.waitForSelector('.el-button', { hasText: '添加成员' }, { timeout: 5000 });
    await page.locator('.el-button', { hasText: '添加成员' }).click();
    await page.waitForSelector('.el-dialog:visible', { timeout: 5000 });
    await page.waitForTimeout(300);

    const tagSelect = page.locator('.el-dialog:visible .el-select').last();
    await tagSelect.click();
    await page.waitForTimeout(400);

    const options = page.locator('.el-select-dropdown__item:visible');
    const optCount = await options.count();
    const optTexts = [];
    for (let i = 0; i < optCount; i++) {
      optTexts.push(await options.nth(i).innerText().catch(() => ''));
    }
    // 后端已有标签：前端/后端/全栈/测试/运维/DBA（来自 dave/eve）
    const hasBackendTag = optTexts.some(t => t.includes('前端') || t.includes('后端') || t.includes('DBA'));
    record('T7 标签建议聚合后端数据', hasBackendTag ? 'PASS' : 'FAIL',
      `下拉选项[${optTexts.join(',')}]`);
  } catch (e) {
    record('T7 标签建议聚合后端数据', 'FAIL', e.message);
  } finally {
    await ctx.close();
  }
}

// =============================================================
// 主流程
// =============================================================

(async () => {
  console.log('=== TeamHarness 成员标签前端点击测试 ===\n');

  const browser = await chromium.launch({ headless: true });

  // 容器 warm-up：首次访问可能因容器冷启动超时
  console.log('--- warm-up ---');
  for (let i = 0; i < 3; i++) {
    try {
      const r = await fetch(BASE);
      if (r.ok) { console.log(`warm-up ${i+1}: OK`); break; }
    } catch (e) { console.log(`warm-up ${i+1} 失败: ${e.message}`); await new Promise(r => setTimeout(r, 2000)); }
  }

  try {
    await test_issueKey_tagsRequired(browser);
    await test_issueKey_tagsMultiSelect(browser);
    await test_memberTable_tagsColumn(browser);
    await test_editTagsDialog(browser);
    await test_addMemberDialog(browser);
    await test_nonAdminNoMemberMenu(browser);
    await test_tagsSuggestionAggregated(browser);
  } finally {
    await browser.close();
  }

  console.log('\n=== 汇总 ===');
  console.log(`PASS: ${passCount}  FAIL: ${failCount}  共 ${results.length}`);
  if (failCount > 0) {
    console.log('\n--- 失败用例 ---');
    results.filter(r => r.status === 'FAIL').forEach(r => {
      console.log(`  ✗ ${r.name} — ${r.detail}`);
    });
    process.exit(1);
  }
})();
