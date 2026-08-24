import jsonata = require('jsonata');
const e = jsonata('1');
// @expect-error evaluate takes the input document as its first argument
e.evaluate();
