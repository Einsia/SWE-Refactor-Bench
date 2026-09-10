import jsonata = require('jsonata');
const e = jsonata('1');
const nothing: void = e.evaluate({}, undefined, function (err, resp) {
  void err.code;
  void err.position;
  void err.token;
  void resp;
});
void nothing;
