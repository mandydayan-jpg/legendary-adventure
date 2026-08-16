const express = require('express');
const bcrypt = require('bcryptjs');
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const DB_PATH = path.join(__dirname, 'db.json');
const UPLOADS_DIR = path.join(__dirname, 'uploads'); // מחוץ ל-public בכוונה - לא מוגש ישירות, רק דרך ה-API המאומת
if (!fs.existsSync(UPLOADS_DIR)) fs.mkdirSync(UPLOADS_DIR, { recursive: true });

const app = express();
app.use(express.json({ limit: '8mb' }));
app.use(express.static(path.join(__dirname, 'public')));

function readDB() {
  if (!fs.existsSync(DB_PATH)) {
    return { users: {}, groups: {}, tasks: {}, notifications: {} };
  }
  const db = JSON.parse(fs.readFileSync(DB_PATH, 'utf8'));
  let migrated = false;
  Object.values(db.groups || {}).forEach(g => {
    if (!Array.isArray(g.admins)) {
      g.admins = g.adminUsername ? [g.adminUsername] : [];
      migrated = true;
    }
  });
  if (migrated) writeDB(db);
  return db;
}

function writeDB(db) {
  fs.writeFileSync(DB_PATH, JSON.stringify(db, null, 2));
}

function genCode() {
  const chars = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';
  let s = '';
  for (let i = 0; i < 6; i++) s += chars[Math.floor(Math.random() * chars.length)];
  return s;
}

function genId() {
  return Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
}

function genToken() {
  return crypto.randomBytes(24).toString('hex');
}

function isAdmin(group, username) {
  return !!(group && group.admins && group.admins.includes(username));
}

function publicUser(u, group) {
  return {
    username: u.username,
    groupId: u.groupId,
    role: isAdmin(group, u.username) ? 'admin' : 'member',
  };
}

function pushNotif(db, groupId, toUser, fromUser, type, taskTitle) {
  if (!toUser || toUser === fromUser) return;
  db.notifications[groupId] = db.notifications[groupId] || [];
  db.notifications[groupId].push({
    id: genId(), toUser, fromUser, type, taskTitle, ts: Date.now(), read: false,
  });
}

// מוודא שהמשתמש הוא באמת מי שהוא טוען שהוא, לפי הטוקן שקיבל בהתחברות.
// מחזיר את שם המשתמש המאומת, או null אם האימות נכשל.
function authenticate(db, username, token) {
  if (!username || !token) return null;
  const user = db.users[username];
  if (!user || !user.sessionToken) return null;
  const a = Buffer.from(user.sessionToken);
  const b = Buffer.from(String(token));
  if (a.length !== b.length) return null;
  if (!crypto.timingSafeEqual(a, b)) return null;
  return username;
}

function requireAuth(req, res, db) {
  const { username, token } = req.body || {};
  const authed = authenticate(db, username, token);
  if (!authed) {
    res.status(401).json({ error: 'ההתחברות פגה, נא להתחבר מחדש' });
    return null;
  }
  return authed;
}

app.post('/api/signup', async (req, res) => {
  const { username, password, mode, groupName, groupCode } = req.body || {};
  if (!username || !password) return res.status(400).json({ error: 'נא למלא שם משתמש וסיסמה' });

  const db = readDB();
  const uname = String(username).trim();
  if (db.users[uname]) return res.status(400).json({ error: 'שם המשתמש הזה כבר תפוס' });

  let groupId;
  if (mode === 'joinGroup') {
    const code = String(groupCode || '').trim().toUpperCase();
    const group = db.groups[code];
    if (!group) return res.status(400).json({ error: 'קוד קבוצה לא נמצא' });
    groupId = code;
    if (!group.members.includes(uname)) group.members.push(uname);
  } else {
    if (!groupName || !groupName.trim()) return res.status(400).json({ error: 'נא להזין שם לקבוצה' });
    groupId = genCode();
    db.groups[groupId] = {
      id: groupId,
      name: groupName.trim(),
      code: groupId,
      adminUsername: uname,
      admins: [uname],
      members: [uname],
    };
    db.tasks[groupId] = [];
    db.notifications[groupId] = [];
  }

  const passwordHash = await bcrypt.hash(password, 10);
  const token = genToken();
  const user = { username: uname, passwordHash, groupId, sessionToken: token };
  db.users[uname] = user;
  writeDB(db);
  res.json({ user: publicUser(user, db.groups[groupId]), group: db.groups[groupId], token });
});

app.post('/api/login', async (req, res) => {
  const { username, password } = req.body || {};
  if (!username || !password) return res.status(400).json({ error: 'נא למלא שם משתמש וסיסמה' });
  const db = readDB();
  const user = db.users[String(username).trim()];
  if (!user) return res.status(400).json({ error: 'שם משתמש לא קיים' });
  const ok = await bcrypt.compare(password, user.passwordHash);
  if (!ok) return res.status(400).json({ error: 'סיסמה שגויה' });
  const token = genToken();
  user.sessionToken = token;
  writeDB(db);
  const group = db.groups[user.groupId];
  res.json({ user: publicUser(user, group), group, token });
});

// שימוש רק לכניסה האוטומטית אחרי הפעם הראשונה, לפי הטוקן שנשמר במכשיר
app.get('/api/whoami/:username', (req, res) => {
  const db = readDB();
  const authed = authenticate(db, req.params.username, req.query.token);
  if (!authed) return res.status(401).json({ error: 'not authenticated' });
  const user = db.users[authed];
  const group = db.groups[user.groupId];
  if (!group) return res.status(404).json({ error: 'group not found' });
  res.json({ user: publicUser(user, group), group, token: user.sessionToken });
});

app.get('/api/state/:groupId', (req, res) => {
  const db = readDB();
  const group = db.groups[req.params.groupId];
  if (!group) return res.status(404).json({ error: 'group not found' });

  const viewer = authenticate(db, req.query.username, req.query.token);
  const viewerIsAdmin = isAdmin(group, viewer);

  const rawTasks = db.tasks[req.params.groupId] || [];
  const tasks = rawTasks.map(t => {
    if (viewerIsAdmin) return t;
    const { photos, ...rest } = t;
    return { ...rest, photoCount: (photos || []).length };
  });

  res.json({
    group,
    tasks,
    notifications: db.notifications[req.params.groupId] || [],
  });
});

app.post('/api/tasks/:groupId/:taskId/photo', (req, res) => {
  const db = readDB();
  const authed = requireAuth(req, res, db);
  if (!authed) return;
  const groupId = req.params.groupId;
  const group = db.groups[groupId];
  if (!group || !group.members.includes(authed)) return res.status(403).json({ error: 'אין הרשאה' });
  const list = db.tasks[groupId] || [];
  const task = list.find(t => t.id === req.params.taskId);
  if (!task) return res.status(404).json({ error: 'task not found' });
  task.photos = task.photos || [];
  if (task.photos.length >= 3) return res.status(400).json({ error: 'כבר יש 3 תמונות במטלה זו' });

  const { imageBase64 } = req.body || {};
  if (!imageBase64) return res.status(400).json({ error: 'לא התקבלה תמונה' });
  const raw = imageBase64.replace(/^data:image\/\w+;base64,/, '');
  const buffer = Buffer.from(raw, 'base64');
  if (buffer.length > 3 * 1024 * 1024) return res.status(400).json({ error: 'התמונה גדולה מדי' });

  const photoId = genId();
  const taskDir = path.join(UPLOADS_DIR, groupId, task.id);
  fs.mkdirSync(taskDir, { recursive: true });
  fs.writeFileSync(path.join(taskDir, photoId + '.jpg'), buffer);
  task.photos.push(photoId);
  writeDB(db);
  res.json({ photos: task.photos });
});

app.get('/api/tasks/:groupId/:taskId/photo/:photoId', (req, res) => {
  const db = readDB();
  const authed = authenticate(db, req.query.username, req.query.token);
  const groupId = req.params.groupId;
  const group = db.groups[groupId];
  if (!group) return res.status(404).json({ error: 'group not found' });
  if (!isAdmin(group, authed)) return res.status(403).json({ error: 'רק מנהל יכול לצפות בתמונות' });
  const filePath = path.join(UPLOADS_DIR, groupId, req.params.taskId, req.params.photoId + '.jpg');
  if (!fs.existsSync(filePath)) return res.status(404).json({ error: 'photo not found' });
  res.sendFile(filePath);
});

app.post('/api/user/change-password', async (req, res) => {
  const db = readDB();
  const authed = requireAuth(req, res, db);
  if (!authed) return;
  const { oldPassword, newPassword } = req.body || {};
  const user = db.users[authed];
  const ok = await bcrypt.compare(oldPassword || '', user.passwordHash);
  if (!ok) return res.status(400).json({ error: 'הסיסמה הנוכחית שגויה' });
  if (!newPassword || newPassword.length < 1) return res.status(400).json({ error: 'נא להזין סיסמה חדשה' });
  user.passwordHash = await bcrypt.hash(newPassword, 10);
  writeDB(db);
  res.json({ ok: true });
});

app.post('/api/group/:groupId/promote', (req, res) => {
  const db = readDB();
  const authed = requireAuth(req, res, db);
  if (!authed) return;
  const groupId = req.params.groupId;
  const group = db.groups[groupId];
  if (!group) return res.status(404).json({ error: 'group not found' });
  if (!isAdmin(group, authed)) return res.status(403).json({ error: 'רק מנהל יכול לעשות זאת' });
  const { targetUsername } = req.body || {};
  if (!group.members.includes(targetUsername)) return res.status(400).json({ error: 'המשתמש אינו חבר בקבוצה' });
  if (!group.admins.includes(targetUsername)) group.admins.push(targetUsername);
  writeDB(db);
  res.json({ group });
});

app.post('/api/group/:groupId/remove-member', (req, res) => {
  const db = readDB();
  const authed = requireAuth(req, res, db);
  if (!authed) return;
  const groupId = req.params.groupId;
  const group = db.groups[groupId];
  if (!group) return res.status(404).json({ error: 'group not found' });
  if (!isAdmin(group, authed)) return res.status(403).json({ error: 'רק מנהל יכול לעשות זאת' });
  const { targetUsername } = req.body || {};
  if (targetUsername === authed) return res.status(400).json({ error: 'להסרה עצמית השתמשו ב"עזיבת קבוצה"' });
  group.members = group.members.filter(m => m !== targetUsername);
  group.admins = group.admins.filter(a => a !== targetUsername);
  delete db.users[targetUsername];
  writeDB(db);
  res.json({ group });
});

app.post('/api/group/:groupId/leave', (req, res) => {
  const db = readDB();
  const authed = requireAuth(req, res, db);
  if (!authed) return;
  const groupId = req.params.groupId;
  const group = db.groups[groupId];
  if (!group) return res.status(404).json({ error: 'group not found' });
  const username = authed;
  const wasOnlyAdmin = isAdmin(group, username) && group.admins.length === 1;
  if (wasOnlyAdmin && group.members.length > 1) {
    return res.status(400).json({ error: 'את/ה המנהל/ת היחיד/ה - קודם מנו מנהל נוסף או מחקו את הקבוצה' });
  }
  group.members = group.members.filter(m => m !== username);
  group.admins = group.admins.filter(a => a !== username);
  delete db.users[username];

  if (group.members.length === 0) {
    delete db.groups[groupId];
    delete db.tasks[groupId];
    delete db.notifications[groupId];
  }
  writeDB(db);
  res.json({ ok: true });
});

app.delete('/api/group/:groupId', (req, res) => {
  const db = readDB();
  const authed = requireAuth(req, res, db);
  if (!authed) return;
  const groupId = req.params.groupId;
  const group = db.groups[groupId];
  if (!group) return res.status(404).json({ error: 'group not found' });
  if (!isAdmin(group, authed)) return res.status(403).json({ error: 'רק מנהל יכול למחוק את הקבוצה' });

  (group.members || []).forEach(m => delete db.users[m]);
  delete db.groups[groupId];
  delete db.tasks[groupId];
  delete db.notifications[groupId];
  writeDB(db);
  res.json({ ok: true });
});

app.post('/api/tasks/:groupId', (req, res) => {
  const db = readDB();
  const authed = requireAuth(req, res, db);
  if (!authed) return;
  const groupId = req.params.groupId;
  const group = db.groups[groupId];
  if (!group) return res.status(404).json({ error: 'group not found' });
  if (!group.members.includes(authed)) return res.status(403).json({ error: 'אינך חבר בקבוצה זו' });
  const { title, note, assignee } = req.body || {};
  if (!title || !title.trim()) return res.status(400).json({ error: 'נא להזין כותרת' });

  const task = {
    id: genId(),
    title: title.trim(),
    note: (note || '').trim(),
    assignee,
    done: false,
    photos: [],
    createdAt: Date.now(),
  };
  db.tasks[groupId] = db.tasks[groupId] || [];
  db.tasks[groupId].push(task);
  pushNotif(db, groupId, assignee, authed, 'assigned', task.title);

  writeDB(db);
  res.json({ task });
});

app.put('/api/tasks/:groupId/:taskId', (req, res) => {
  const db = readDB();
  const authed = requireAuth(req, res, db);
  if (!authed) return;
  const groupId = req.params.groupId;
  const group = db.groups[groupId];
  if (!group) return res.status(404).json({ error: 'group not found' });
  if (!group.members.includes(authed)) return res.status(403).json({ error: 'אינך חבר בקבוצה זו' });
  const list = db.tasks[groupId] || [];
  const idx = list.findIndex(t => t.id === req.params.taskId);
  if (idx === -1) return res.status(404).json({ error: 'task not found' });

  const prev = list[idx];
  const { title, note, assignee, done } = req.body || {};
  const updated = { ...prev };
  if (title !== undefined) updated.title = title.trim();
  if (note !== undefined) updated.note = note.trim();
  if (assignee !== undefined) updated.assignee = assignee;
  if (done !== undefined) updated.done = done;
  list[idx] = updated;
  db.tasks[groupId] = list;

  if (assignee !== undefined && assignee !== prev.assignee) {
    pushNotif(db, groupId, assignee, authed, 'assigned', updated.title);
  }
  if (done === true && prev.done === false) {
    (group.admins || []).forEach(adminUser => pushNotif(db, groupId, adminUser, authed, 'done', updated.title));
  }

  writeDB(db);
  res.json({ task: updated });
});

app.delete('/api/tasks/:groupId/:taskId', (req, res) => {
  const db = readDB();
  const authed = requireAuth(req, res, db);
  if (!authed) return;
  const groupId = req.params.groupId;
  const group = db.groups[groupId];
  if (!group || !group.members.includes(authed)) return res.status(403).json({ error: 'אין הרשאה' });
  db.tasks[groupId] = (db.tasks[groupId] || []).filter(t => t.id !== req.params.taskId);
  writeDB(db);
  try {
    const taskDir = path.join(UPLOADS_DIR, groupId, req.params.taskId);
    if (fs.existsSync(taskDir)) fs.rmSync(taskDir, { recursive: true, force: true });
  } catch (e) {}
  res.json({ ok: true });
});

app.post('/api/notifications/:groupId/read', (req, res) => {
  const db = readDB();
  const authed = requireAuth(req, res, db);
  if (!authed) return;
  const groupId = req.params.groupId;
  db.notifications[groupId] = (db.notifications[groupId] || []).map(n =>
    n.toUser === authed ? { ...n, read: true } : n
  );
  writeDB(db);
  res.json({ ok: true });
});

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => console.log('Server running on port ' + PORT));
