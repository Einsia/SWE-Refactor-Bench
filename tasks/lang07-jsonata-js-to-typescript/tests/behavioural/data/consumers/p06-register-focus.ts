import jsonata = require('jsonata');
const e = jsonata('$f(1)');
e.registerFunction('f', function (this: jsonata.Focus, n: number): number {
  void this.input;
  void this.environment.lookup('x');
  this.environment.bind('y', 2);
  void this.environment.timestamp;
  void this.environment.async;
  return n;
});
e.registerFunction('g', () => 1, '<n:n>');
