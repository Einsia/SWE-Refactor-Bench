import jsonata = require('jsonata');
const e = jsonata('a.', { recover: true });
const diagnostics = e.errors();
if (diagnostics !== undefined) {
  void diagnostics[0].code;
  void diagnostics[0].position;
  void diagnostics[0].token;
}
