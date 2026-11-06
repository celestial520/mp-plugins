/**
 * WorkBuddy 长效刷新令牌（refresh token）一次性导出工具
 *
 * 用途：把本机已登录 WorkBuddy 客户端的 refresh token 解密后写入本地文件，
 *       供跑在 NAS / 服务器上的 Docker 签到容器使用。
 *
 * 运行（Windows PowerShell 或 CMD）：
 *   set ELECTRON_RUN_AS_NODE=1
 *   "C:\Program Files\WorkBuddy\WorkBuddy.exe" tools\export_token.cjs
 *
 * 说明：
 *  - 客户端 5.6.2+ 的登录态是 AES-256-GCM 加密信封，解密密钥由客户端运行时提供，
 *    因此必须用 WorkBuddy.exe 自身以 Node 模式执行本脚本（普通 node 无法取到密钥）。
 *  - 全程只在本机内存里解密，绝不向终端、日志输出任何令牌明文。
 *  - 输出文件等同账号钥匙，请妥善保管，不要提交到 git / 上传到公网。
 *
 * 可覆盖的环境变量：
 *   WORKBUDDY_AUTH_FILE   登录态 .info 文件路径
 *   WORKBUDDY_OUT_FILE    令牌输出文件（默认 ../data/refresh_token）
 */
'use strict';

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

function defaultAuthFile() {
  const base = process.env.LOCALAPPDATA ||
    path.join(process.env.USERPROFILE || '', 'AppData', 'Local');
  return path.join(base, 'CodeBuddyExtension', 'Data', 'Public', 'auth', 'workbuddy-desktop.info');
}

const AUTH_FILE = process.env.WORKBUDDY_AUTH_FILE || defaultAuthFile();
const OUT_FILE = process.env.WORKBUDDY_OUT_FILE ||
  path.join(__dirname, '..', 'data', 'refresh_token');
const META_FILE = path.join(path.dirname(OUT_FILE), 'account.json');

function die(msg) {
  console.error('[export] ' + msg);
  process.exit(1);
}

// sym-v1 / field framing 的 AAD，与客户端 buildAuthenticatedContextAad 保持一致
function buildAad(keyId, suite) {
  const lp = (s) => {
    const b = Buffer.from(s, 'utf8');
    const n = Buffer.alloc(4);
    n.writeUInt32BE(b.length);
    return Buffer.concat([n, b]);
  };
  const u32 = (n) => {
    const b = Buffer.alloc(4);
    b.writeUInt32BE(n);
    return b;
  };
  return Buffer.concat([
    Buffer.from('WB-AAD\0', 'ascii'),
    Buffer.from([1]),
    lp('WBEV1'),
    lp('sym-v1'),
    u32(suite),
    lp(keyId),
    Buffer.from([2]),
    Buffer.from([0]),
    Buffer.from([0]),
  ]);
}

function getProtectorKey() {
  let binding;
  try {
    binding = process._linkedBinding('electron_browser_workbuddy_storage');
  } catch (e) {
    die('无法访问客户端密钥。请用 ELECTRON_RUN_AS_NODE=1 加 WorkBuddy.exe 运行本脚本，而不是普通 node。');
  }
  const payload = JSON.parse(binding.loggerGet());
  const secret = payload.atRestSecretKey;
  if (!secret) die('客户端未提供 atRestSecretKey，请确认 WorkBuddy 已登录。');
  const key = crypto.createHash('sha256').update(secret, 'utf8').digest();
  return { key, keyId: crypto.createHash('sha256').update(key).digest('hex').slice(0, 16) };
}

function openEnvelope(wrapper, protector) {
  const env = JSON.parse(Buffer.from(wrapper.envelope, 'base64').toString('utf8'));
  if (env.suite !== 1) die('不支持的信封 suite=' + env.suite);
  if (env.keyId !== protector.keyId) {
    die('keyId 不匹配（客户端密钥已更换？请重新登录 WorkBuddy 后再试）');
  }
  const nonce = Buffer.from(env.nonce, 'base64');
  const tag = Buffer.from(env.authTag, 'base64');
  const ct = Buffer.from(env.ciphertext, 'base64');
  const d = crypto.createDecipheriv('aes-256-gcm', protector.key, nonce, { authTagLength: 16 });
  d.setAAD(buildAad(env.keyId, env.suite));
  d.setAuthTag(tag);
  return Buffer.concat([d.update(ct), d.final()]).toString('utf8');
}

function extractToken(plain) {
  const t = plain.trim();
  // 兼容明文字符串 / JSON 包装两种形态
  if (t.startsWith('{')) {
    try {
      const o = JSON.parse(t);
      const v = o.refreshToken || o.refresh_token || o.token || o.value;
      if (typeof v === 'string' && v.length) return v.trim();
    } catch (e) { /* 不是 JSON，按原样使用 */ }
  }
  return t;
}

function main() {
  if (!fs.existsSync(AUTH_FILE)) die('找不到登录态文件：' + AUTH_FILE);
  const info = JSON.parse(fs.readFileSync(AUTH_FILE, 'utf8'));
  const auth = info.auth || {};
  const wrapper = auth.refreshToken;
  if (!wrapper) die('登录态里没有 refreshToken，请先在 WorkBuddy 客户端登录。');

  let plain;
  if (typeof wrapper === 'string') {
    plain = wrapper; // 5.6.2 之前的明文登录态
  } else if (wrapper && wrapper.$wbEncrypted === 1 && wrapper.envelope) {
    plain = openEnvelope(wrapper, getProtectorKey());
  } else {
    die('无法识别的 refreshToken 形态');
  }

  const token = extractToken(plain);
  if (!token || token.length < 40) die('解出的令牌异常（长度 ' + token.length + '）');

  fs.mkdirSync(path.dirname(OUT_FILE), { recursive: true });
  // 原子写入，避免写一半被容器读到
  const tmp = OUT_FILE + '.tmp';
  fs.writeFileSync(tmp, token, { encoding: 'utf8', mode: 0o600 });
  fs.renameSync(tmp, OUT_FILE);
  try { fs.chmodSync(OUT_FILE, 0o600); } catch (e) { /* Windows 可能不支持 */ }

  // 只保存非敏感的域名信息，供容器决定签到接口地址
  fs.writeFileSync(META_FILE, JSON.stringify({
    domain: auth.domain || 'www.workbuddy.cn',
    exported_at: new Date().toISOString(),
  }, null, 2), { encoding: 'utf8', mode: 0o600 });

  console.log('[export] 成功：已写入 ' + OUT_FILE);
  console.log('[export] 令牌长度=' + token.length +
    ' 指纹=' + crypto.createHash('sha256').update(token).digest('hex').slice(0, 8) +
    '（明文未输出）');
  console.log('[export] 接口域名=' + (auth.domain || 'www.workbuddy.cn'));
}

main();
