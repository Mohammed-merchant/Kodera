/**
 * Kodera: phone bookings -> this Google Sheet.
 *
 * Paste this whole file into Extensions > Apps Script (replace everything there), set SECRET,
 * run setup() once, then Deploy > New deployment > Web app. Guide: docs/google_sheet.md
 */

// Must be the SAME value as GOOGLE_SHEET_SECRET in the app's .env on the VPS.
const SECRET = 'PASTE_GOOGLE_SHEET_SECRET_HERE';

const SHEET_NAME = 'Bookings';
const HEADERS = ['Received at', 'Status', 'Patient name', 'Phone', 'Date', 'Time',
                 'Reason', 'Call ID', 'Cancelled at'];
const COL = { status: 2, phone: 4, date: 5, time: 6, callId: 8, cancelledAt: 9 };
const TIMEZONE = 'Asia/Kolkata';

/** Run once by hand: creates the "Bookings" tab with headers and formatting. */
function setup() {
  Logger.log('Sheet ready: ' + getSheet_().getName());
}

/** The app sends each booking / cancellation here as JSON. */
function doPost(e) {
  const lock = LockService.getScriptLock();
  lock.waitLock(20000); // two calls booking at once must not write the same row
  try {
    const data = JSON.parse(e.postData.contents);
    if (data.secret !== SECRET) return json_({ ok: false, error: 'bad secret' });
    const sheet = getSheet_();
    if (data.action === 'booked') return json_(addBooking_(sheet, data));
    if (data.action === 'cancelled') return json_(cancelBooking_(sheet, data));
    return json_({ ok: false, error: 'unknown action' });
  } catch (err) {
    return json_({ ok: false, error: String(err) });
  } finally {
    lock.releaseLock();
  }
}

/** Open the web app URL in a browser to check it is live. */
function doGet() {
  return ContentService.createTextOutput('Kodera booking sheet is running.');
}

function addBooking_(sheet, d) {
  // The app retries if Google was slow: never add the same booking twice.
  if (findRow_(sheet, d, 'Booked', d.call_id)) return { ok: true, duplicate: true };
  sheet.appendRow([
    now_(), 'Booked', d.patient_name || '', d.phone || '', d.date || '', d.time || '',
    d.reason || '', d.call_id || '', '',
  ]);
  const row = sheet.getLastRow();
  sheet.getRange(row, COL.status).setBackground('#d9ead3');
  return { ok: true, row: row };
}

function cancelBooking_(sheet, d) {
  const row = findRow_(sheet, d, 'Booked');
  if (row) {
    sheet.getRange(row, COL.status).setValue('Cancelled').setBackground('#f4cccc');
    sheet.getRange(row, COL.cancelledAt).setValue(now_());
    return { ok: true, row: row };
  }
  if (findRow_(sheet, d, 'Cancelled')) return { ok: true, duplicate: true };
  // Booked before the sheet existed: still record the cancellation.
  sheet.appendRow([now_(), 'Cancelled', '', d.phone || '', d.date || '', d.time || '',
                   '', '', now_()]);
  sheet.getRange(sheet.getLastRow(), COL.status).setBackground('#f4cccc');
  return { ok: true, row: sheet.getLastRow() };
}

/** Row number of the booking with this phone, date and time (and status), or 0. */
function findRow_(sheet, d, status, callId) {
  const last = sheet.getLastRow();
  if (last < 2) return 0;
  const rows = sheet.getRange(2, 1, last - 1, HEADERS.length).getDisplayValues();
  for (let i = rows.length - 1; i >= 0; i--) {
    const r = rows[i];
    if (r[COL.status - 1] === status && r[COL.phone - 1] === String(d.phone) &&
        r[COL.date - 1] === String(d.date) && r[COL.time - 1] === String(d.time) &&
        (!callId || r[COL.callId - 1] === String(callId))) {
      return i + 2;
    }
  }
  return 0;
}

function getSheet_() {
  const book = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = book.getSheetByName(SHEET_NAME);
  if (!sheet) sheet = book.insertSheet(SHEET_NAME);
  if (sheet.getLastRow() === 0) {
    sheet.appendRow(HEADERS);
    sheet.getRange(1, 1, 1, HEADERS.length)
      .setFontWeight('bold').setBackground('#1f4e79').setFontColor('#ffffff');
    sheet.setFrozenRows(1);
    // Keep phone, date and time as plain text (no "+91..." turned into a number).
    sheet.getRange('D:F').setNumberFormat('@');
    sheet.setColumnWidths(1, HEADERS.length, 140);
    sheet.setColumnWidth(7, 260);
  }
  return sheet;
}

function now_() {
  return Utilities.formatDate(new Date(), TIMEZONE, 'yyyy-MM-dd HH:mm:ss');
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
