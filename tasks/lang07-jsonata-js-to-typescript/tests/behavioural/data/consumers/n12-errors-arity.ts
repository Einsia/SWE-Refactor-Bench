import jsonata = require('jsonata');
const e = jsonata('1');
// @expect-error errors() takes no arguments
e.errors(1);
