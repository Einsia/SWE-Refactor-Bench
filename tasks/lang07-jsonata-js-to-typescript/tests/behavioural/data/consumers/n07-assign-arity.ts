import jsonata = require('jsonata');
const e = jsonata('1');
// @expect-error assign takes a name and a value
e.assign('x');
