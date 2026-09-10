import jsonata = require('jsonata');
const e = jsonata('1');
// @expect-error the implementation is a function
e.registerFunction('f', 42);
