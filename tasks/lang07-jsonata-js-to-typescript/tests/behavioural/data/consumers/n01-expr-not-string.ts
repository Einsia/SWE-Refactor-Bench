import jsonata = require('jsonata');
// @expect-error the expression argument is a string, not a number
jsonata(42);
