import jsonata = require('jsonata');
function report(err: jsonata.JsonataError): string {
  return err.message + err.code + String(err.position) + err.token + err.name;
}
void report;
