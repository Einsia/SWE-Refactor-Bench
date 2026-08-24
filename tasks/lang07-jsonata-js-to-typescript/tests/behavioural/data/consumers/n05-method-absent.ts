import jsonata = require('jsonata');
const e = jsonata('1');
// @expect-error Expression declares no such method
e.nosuchmethod();
