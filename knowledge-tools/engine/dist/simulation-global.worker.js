/* GPL-3.0-or-later. Contains kachi-dev/uma-tools@4401e25987d9c78707f2d3f8a9dcd02c1c56529c. Source and patches accompany this build. */
(() => {
  var __defProp = Object.defineProperty;
  var __getOwnPropDesc = Object.getOwnPropertyDescriptor;
  var __getOwnPropNames = Object.getOwnPropertyNames;
  var __hasOwnProp = Object.prototype.hasOwnProperty;
  var __esm = (fn, res) => function __init() {
    return fn && (res = (0, fn[__getOwnPropNames(fn)[0]])(fn = 0)), res;
  };
  var __export = (target, all) => {
    for (var name in all)
      __defProp(target, name, { get: all[name], enumerable: true });
  };
  var __copyProps = (to, from, except, desc) => {
    if (from && typeof from === "object" || typeof from === "function") {
      for (let key of __getOwnPropNames(from))
        if (!__hasOwnProp.call(to, key) && key !== except)
          __defProp(to, key, { get: () => from[key], enumerable: !(desc = __getOwnPropDesc(from, key)) || desc.enumerable });
    }
    return to;
  };
  var __toCommonJS = (mod) => __copyProps(__defProp({}, "__esModule", { value: true }), mod);

  // vendor/kachi-uma-tools/uma-skill-tools/Region.ts
  var Region, RegionList;
  var init_Region = __esm({
    "vendor/kachi-uma-tools/uma-skill-tools/Region.ts"() {
      Region = class _Region {
        constructor(start, end) {
          this.start = start;
          this.end = end;
        }
        intersect(other) {
          const start = Math.max(this.start, other.start);
          const end = Math.min(this.end, other.end);
          if (end <= start) {
            return new _Region(-1, -1);
          } else {
            return new _Region(start, end);
          }
        }
        fullyContains(other) {
          return this.start <= other.start && this.end >= other.end;
        }
      };
      RegionList = class _RegionList extends Array {
        rmap(f) {
          const out = new _RegionList();
          this.forEach((r) => {
            const newr = f(r);
            if (Array.isArray(newr)) {
              newr.forEach((nr) => {
                if (nr.start > -1) {
                  out.push(nr);
                }
              });
            } else if (newr.start > -1) {
              out.push(newr);
            }
          });
          return out;
        }
        union(other) {
          const u = [];
          const r = new _RegionList();
          u.push.apply(u, this);
          u.push.apply(u, other);
          if (u.length == 0) {
            return r;
          }
          u.sort((a, b) => a.start - b.start);
          r.push(u.reduce((a, b) => {
            if (a.fullyContains(b)) {
              return a;
            } else if (a.start <= b.start && b.start < a.end) {
              return new Region(a.start, b.end);
            } else if (a.start < b.end && b.end <= a.end) {
              return new Region(b.start, a.end);
            } else {
              r.push(a);
              return b;
            }
          }));
          return r;
        }
      };
    }
  });

  // vendor/kachi-uma-tools/uma-skill-tools/ActivationSamplePolicy.ts
  var ActivationSamplePolicy_exports = {};
  __export(ActivationSamplePolicy_exports, {
    AllCornerRandomPolicy: () => AllCornerRandomPolicy,
    DistributionRandomPolicy: () => DistributionRandomPolicy,
    ErlangRandomPolicy: () => ErlangRandomPolicy,
    ImmediatePolicy: () => ImmediatePolicy,
    LogNormalRandomPolicy: () => LogNormalRandomPolicy,
    RandomPolicy: () => RandomPolicy,
    StraightRandomPolicy: () => StraightRandomPolicy,
    UniformRandomPolicy: () => UniformRandomPolicy,
    createFixedPositionPolicy: () => createFixedPositionPolicy
  });
  function createFixedPositionPolicy(position) {
    return Object.freeze({
      sample(_regions, nsamples, _rng) {
        const samples = [];
        for (let i = 0; i < nsamples; ++i) {
          samples.push(new Region(position, position + 10));
        }
        return samples;
      },
      reconcile(_other) {
        return this;
      },
      reconcileImmediate(_) {
        return this;
      },
      reconcileDistributionRandom(_) {
        return this;
      },
      reconcileRandom(_) {
        return this;
      },
      reconcileStraightRandom(_) {
        return this;
      },
      reconcileAllCornerRandom(_) {
        return this;
      }
    });
  }
  var ImmediatePolicy, RandomPolicy, DistributionRandomPolicy, UniformRandomPolicy, LogNormalRandomPolicy, ErlangRandomPolicy, StraightRandomPolicy, AllCornerRandomPolicy;
  var init_ActivationSamplePolicy = __esm({
    "vendor/kachi-uma-tools/uma-skill-tools/ActivationSamplePolicy.ts"() {
      init_Region();
      ImmediatePolicy = Object.freeze({
        sample(regions, _0, _1) {
          return regions.slice(0, 1);
        },
        reconcile(other) {
          return other.reconcileImmediate(this);
        },
        reconcileImmediate(other) {
          return other;
        },
        reconcileDistributionRandom(other) {
          return other;
        },
        reconcileRandom(other) {
          return other;
        },
        reconcileStraightRandom(other) {
          return other;
        },
        reconcileAllCornerRandom(other) {
          return other;
        }
      });
      RandomPolicy = Object.freeze({
        sample(regions, nsamples, rng) {
          if (regions.length == 0) {
            return [];
          }
          let acc = 0;
          const weights = regions.map((r) => acc += r.end - r.start);
          const samples = [];
          for (let i = 0; i < nsamples; ++i) {
            const threshold = rng.uniform(acc);
            const region = regions.find((_, i2) => weights[i2] > threshold);
            samples.push(region.start + rng.uniform(region.end - region.start - 10));
          }
          return samples.map((pos) => new Region(pos, pos + 10));
        },
        reconcile(other) {
          return other.reconcileRandom(this);
        },
        reconcileImmediate(_) {
          return this;
        },
        reconcileDistributionRandom(other) {
          return this;
        },
        reconcileRandom(other) {
          return other;
        },
        reconcileStraightRandom(other) {
          return other;
        },
        reconcileAllCornerRandom(other) {
          return other;
        }
      });
      DistributionRandomPolicy = class {
        sample(regions, nsamples, rng) {
          if (regions.length == 0) {
            return [];
          }
          const range = regions.reduce((acc, r) => acc + r.end - r.start, 0);
          const rs = regions.slice().sort((a, b) => a.start - b.start);
          const randoms = this.distribution(range, nsamples, rng);
          const samples = [];
          for (let i = 0; i < nsamples; ++i) {
            let pos = randoms[i];
            for (let j = 0; ; j++) {
              pos += rs[j].start;
              if (pos > rs[j].end) {
                pos -= rs[j].end;
              } else {
                samples.push(new Region(pos, rs[j].end));
                break;
              }
            }
          }
          return samples;
        }
        reconcile(other) {
          return other.reconcileDistributionRandom(this);
        }
        reconcileImmediate(_) {
          return this;
        }
        reconcileDistributionRandom(other) {
          return this;
        }
        // this is probably not exactly the right thing to do either, but the true random conditions do need to place a fixed trigger
        // statically ahead of time, uninfluenced by us. this means that the only alternatives are 1) this condition is coincidentally
        // fulfilled during the static random trigger or 2) the skill does not activate at all.
        // since the latter is not particularly interesting, it's safe to just ignore this sample policy and use only the true random one.
        reconcileRandom(other) {
          return other;
        }
        reconcileStraightRandom(other) {
          return other;
        }
        reconcileAllCornerRandom(other) {
          return other;
        }
      };
      UniformRandomPolicy = class extends DistributionRandomPolicy {
        constructor() {
          super();
        }
        distribution(upper, nsamples, rng) {
          const nums = [];
          for (let i = 0; i < nsamples; ++i) {
            nums.push(rng.uniform(upper));
          }
          return nums;
        }
      };
      LogNormalRandomPolicy = class extends DistributionRandomPolicy {
        constructor(mu, sigma) {
          super();
          this.mu = mu;
          this.sigma = sigma;
        }
        distribution(upper, nsamples, rng) {
          let nums = [], min = Infinity, max = 0;
          const halfn = Math.ceil(nsamples / 2);
          for (let i = 0; i < halfn; ++i) {
            let x, y, r2;
            do {
              x = rng.random() * 2 - 1;
              y = rng.random() * 2 - 1;
              r2 = x * x + y * y;
            } while (r2 == 0 || r2 >= 1);
            const m = Math.sqrt(-2 * Math.log(r2) / r2) * this.sigma;
            const a = Math.exp(x * m + this.mu);
            const b = Math.exp(y * m + this.mu);
            min = Math.min(min, a, b);
            max = Math.max(max, a, b);
            nums.push(a, b);
          }
          const range = max - min;
          return nums.map((n) => Math.floor(upper * (n - min) / range));
        }
      };
      ErlangRandomPolicy = class extends DistributionRandomPolicy {
        constructor(k, lambda) {
          super();
          this.k = k;
          this.lambda = lambda;
        }
        distribution(upper, nsamples, rng) {
          const nums = [];
          let min = Infinity, max = 0;
          for (let i = 0; i < nsamples; ++i) {
            let u = 1;
            for (let j = 0; j < this.k; ++j) {
              u *= rng.random();
            }
            const n = -Math.log(u) / this.lambda;
            min = Math.min(min, n);
            max = Math.max(max, n);
            nums.push(n);
          }
          if (nsamples == 1) {
            const scale = 18;
            return nums.map((n) => Math.floor(upper * Math.min(n / scale, 1)));
          }
          const range = max - min;
          return nums.map((n) => Math.floor(upper * (n - min) / range));
        }
      };
      StraightRandomPolicy = Object.freeze({
        sample(regions, nsamples, rng) {
          if (regions.length == 0) {
            return [];
          }
          const samples = [];
          for (let i = 0; i < nsamples; ++i) {
            const r = regions[rng.uniform(regions.length)];
            samples.push(r.start + rng.uniform(r.end - r.start - 10));
          }
          return samples.map((pos) => new Region(pos, pos + 10));
        },
        reconcile(other) {
          return other.reconcileStraightRandom(this);
        },
        reconcileImmediate(_) {
          return this;
        },
        reconcileDistributionRandom(_) {
          return this;
        },
        reconcileRandom(_) {
          return this;
        },
        reconcileStraightRandom(other) {
          return other;
        },
        reconcileAllCornerRandom(other) {
          throw new Error("cannot reconcile StraightRandomPolicy with AllCornerRandomPolicy");
        }
      });
      AllCornerRandomPolicy = Object.freeze({
        placeTriggers(regions, rng) {
          const triggers = [];
          const candidates = regions.slice();
          candidates.sort((a, b) => a.start - b.start);
          while (triggers.length < 4 && candidates.length > 0) {
            const ci = rng.uniform(candidates.length);
            const c = candidates[ci];
            const start = c.start + rng.uniform(c.end - c.start - 10);
            if (start + 20 <= c.end) {
              candidates.splice(ci, 1, new Region(start + 10, c.end));
            } else {
              candidates.splice(ci, 1);
            }
            candidates.splice(0, ci);
            triggers.push(start);
          }
          return new Region(triggers[0], triggers[0] + 10);
        },
        sample(regions, nsamples, rng) {
          const samples = [];
          for (let i = 0; i < nsamples; ++i) {
            samples.push(this.placeTriggers(regions, rng));
          }
          return samples;
        },
        reconcile(other) {
          return other.reconcileAllCornerRandom(this);
        },
        reconcileImmediate(_) {
          return this;
        },
        reconcileDistributionRandom(_) {
          return this;
        },
        reconcileRandom(_) {
          return this;
        },
        reconcileStraightRandom(_) {
          throw new Error("cannot reconcile StraightRandomPolicy with AllCornerRandomPolicy");
        },
        reconcileAllCornerRandom(_) {
          return this;
        }
      });
    }
  });

  // reference-assert:assert
  function strict(value, message) {
    if (!value)
      throw new Error(message || "Reference model assertion failed");
  }

  // vendor/kachi-uma-tools/uma-skill-tools/HorseTypes.ts
  var Strategy = /* @__PURE__ */ ((Strategy2) => {
    Strategy2[Strategy2["Nige"] = 1] = "Nige";
    Strategy2[Strategy2["Senkou"] = 2] = "Senkou";
    Strategy2[Strategy2["Sasi"] = 3] = "Sasi";
    Strategy2[Strategy2["Oikomi"] = 4] = "Oikomi";
    Strategy2[Strategy2["Oonige"] = 5] = "Oonige";
    return Strategy2;
  })(Strategy || {});
  var StrategyHelpers;
  ((StrategyHelpers3) => {
    function assertIsStrategy(strategy) {
      strict(Strategy.hasOwnProperty(strategy));
    }
    StrategyHelpers3.assertIsStrategy = assertIsStrategy;
    function strategyMatches(s1, s2) {
      return s1 == s2 || s1 == 1 /* Nige */ && s2 == 5 /* Oonige */ || s1 == 5 /* Oonige */ && s2 == 1 /* Nige */;
    }
    StrategyHelpers3.strategyMatches = strategyMatches;
  })(StrategyHelpers || (StrategyHelpers = {}));

  // vendor/kachi-uma-tools/umalator-global/course_data.json
  var course_data_default = {
    "10101": {
      corners: [
        {
          length: 275,
          start: 400
        },
        {
          length: 275,
          start: 675
        }
      ],
      course: 1,
      courseSetStatus: [],
      distance: 1200,
      distanceType: 1,
      finishTimeMax: 71e4,
      finishTimeMin: 675e3,
      laneMax: 13500,
      raceTrackId: 10001,
      slopes: [],
      straights: [
        {
          end: 400,
          frontType: 2,
          start: 0
        },
        {
          end: 1200,
          frontType: 1,
          start: 950
        }
      ],
      surface: 1,
      turn: 1
    },
    "10102": {
      corners: [
        {
          length: 275,
          start: 150
        },
        {
          length: 275,
          start: 700
        },
        {
          length: 275,
          start: 975
        }
      ],
      course: 1,
      courseSetStatus: [],
      distance: 1500,
      distanceType: 2,
      finishTimeMax: 95e4,
      finishTimeMin: 879e3,
      laneMax: 13500,
      raceTrackId: 10001,
      slopes: [],
      straights: [
        {
          end: 700,
          frontType: 2,
          start: 425
        },
        {
          end: 1500,
          frontType: 1,
          start: 1250
        }
      ],
      surface: 1,
      turn: 1
    },
    "10103": {
      corners: [
        {
          length: 275,
          start: 175
        },
        {
          length: 275,
          start: 450
        },
        {
          length: 275,
          start: 1e3
        },
        {
          length: 275,
          start: 1275
        }
      ],
      course: 1,
      courseSetStatus: [],
      distance: 1800,
      distanceType: 2,
      finishTimeMax: 11e5,
      finishTimeMin: 1044e3,
      laneMax: 13500,
      raceTrackId: 10001,
      slopes: [],
      straights: [
        {
          end: 175,
          frontType: 1,
          start: 0
        },
        {
          end: 1e3,
          frontType: 2,
          start: 725
        },
        {
          end: 1800,
          frontType: 1,
          start: 1550
        }
      ],
      surface: 1,
      turn: 1
    },
    "10104": {
      corners: [
        {
          length: 275,
          start: 375
        },
        {
          length: 275,
          start: 650
        },
        {
          length: 275,
          start: 1200
        },
        {
          length: 275,
          start: 1475
        }
      ],
      course: 1,
      courseSetStatus: [
        3
      ],
      distance: 2e3,
      distanceType: 3,
      finishTimeMax: 123e4,
      finishTimeMin: 1171e3,
      laneMax: 13500,
      raceTrackId: 10001,
      slopes: [],
      straights: [
        {
          end: 375,
          frontType: 1,
          start: 0
        },
        {
          end: 1200,
          frontType: 2,
          start: 925
        },
        {
          end: 2e3,
          frontType: 1,
          start: 1750
        }
      ],
      surface: 1,
      turn: 1
    },
    "10105": {
      corners: [
        {
          length: 275,
          start: 175
        },
        {
          length: 275,
          start: 450
        },
        {
          length: 275,
          start: 975
        },
        {
          length: 275,
          start: 1250
        },
        {
          length: 275,
          start: 1800
        },
        {
          length: 275,
          start: 2075
        }
      ],
      course: 1,
      courseSetStatus: [
        2
      ],
      distance: 2600,
      distanceType: 4,
      finishTimeMax: 165e4,
      finishTimeMin: 1576e3,
      laneMax: 13500,
      raceTrackId: 10001,
      slopes: [],
      straights: [
        {
          end: 175,
          frontType: 2,
          start: 0
        },
        {
          end: 975,
          frontType: 1,
          start: 725
        },
        {
          end: 1800,
          frontType: 2,
          start: 1525
        },
        {
          end: 2600,
          frontType: 1,
          start: 2350
        }
      ],
      surface: 1,
      turn: 1
    },
    "10106": {
      corners: [
        {
          length: 230,
          start: 280
        },
        {
          length: 230,
          start: 510
        }
      ],
      course: 1,
      courseSetStatus: [],
      distance: 1e3,
      distanceType: 1,
      finishTimeMax: 63e4,
      finishTimeMin: 574e3,
      laneMax: 11e3,
      raceTrackId: 10001,
      slopes: [],
      straights: [
        {
          end: 280,
          frontType: 2,
          start: 0
        },
        {
          end: 1e3,
          frontType: 1,
          start: 740
        }
      ],
      surface: 2,
      turn: 1
    },
    "10107": {
      corners: [
        {
          length: 230,
          start: 240
        },
        {
          length: 230,
          start: 470
        },
        {
          length: 230,
          start: 980
        },
        {
          length: 230,
          start: 1210
        }
      ],
      course: 1,
      courseSetStatus: [
        1
      ],
      distance: 1700,
      distanceType: 2,
      finishTimeMax: 113e4,
      finishTimeMin: 1014e3,
      laneMax: 11e3,
      raceTrackId: 10001,
      slopes: [],
      straights: [
        {
          end: 240,
          frontType: 1,
          start: 0
        },
        {
          end: 980,
          frontType: 2,
          start: 700
        },
        {
          end: 1700,
          frontType: 1,
          start: 1440
        }
      ],
      surface: 2,
      turn: 1
    },
    "10108": {
      corners: [
        {
          length: 230,
          start: 200
        },
        {
          length: 230,
          start: 430
        },
        {
          length: 230,
          start: 940
        },
        {
          length: 230,
          start: 1170
        },
        {
          length: 230,
          start: 1680
        },
        {
          length: 230,
          start: 1910
        }
      ],
      course: 1,
      courseSetStatus: [],
      distance: 2400,
      distanceType: 3,
      finishTimeMax: 156e4,
      finishTimeMin: 1491e3,
      laneMax: 11e3,
      raceTrackId: 10001,
      slopes: [],
      straights: [
        {
          end: 200,
          frontType: 2,
          start: 0
        },
        {
          end: 940,
          frontType: 1,
          start: 660
        },
        {
          end: 1680,
          frontType: 2,
          start: 1408
        },
        {
          end: 2400,
          frontType: 1,
          start: 2140
        }
      ],
      surface: 2,
      turn: 1
    },
    "10201": {
      corners: [
        {
          length: 220,
          start: 310
        },
        {
          length: 220,
          start: 530
        }
      ],
      course: 1,
      courseSetStatus: [],
      distance: 1e3,
      distanceType: 1,
      finishTimeMax: 57e4,
      finishTimeMin: 547e3,
      laneMax: 14700,
      raceTrackId: 10002,
      slopes: [
        {
          length: 555,
          slope: 1e4,
          start: 0
        }
      ],
      straights: [
        {
          end: 310,
          frontType: 2,
          start: 0
        },
        {
          end: 1e3,
          frontType: 1,
          start: 750
        }
      ],
      surface: 1,
      turn: 1
    },
    "10202": {
      corners: [
        {
          length: 220,
          start: 510
        },
        {
          length: 220,
          start: 730
        }
      ],
      course: 1,
      courseSetStatus: [],
      distance: 1200,
      distanceType: 1,
      finishTimeMax: 71e4,
      finishTimeMin: 675e3,
      laneMax: 14700,
      raceTrackId: 10002,
      slopes: [
        {
          length: 755,
          slope: 1e4,
          start: 0
        }
      ],
      straights: [
        {
          end: 510,
          frontType: 2,
          start: 0
        },
        {
          end: 1200,
          frontType: 1,
          start: 950
        }
      ],
      surface: 1,
      turn: 1
    },
    "10203": {
      corners: [
        {
          length: 220,
          start: 320
        },
        {
          length: 220,
          start: 540
        },
        {
          length: 220,
          start: 1110
        },
        {
          length: 220,
          start: 1330
        }
      ],
      course: 1,
      courseSetStatus: [
        3
      ],
      distance: 1800,
      distanceType: 2,
      finishTimeMax: 11e5,
      finishTimeMin: 1044e3,
      laneMax: 14700,
      raceTrackId: 10002,
      slopes: [
        {
          length: 200,
          slope: -1e4,
          start: 220
        },
        {
          length: 720,
          slope: 1e4,
          start: 665
        }
      ],
      straights: [
        {
          end: 320,
          frontType: 1,
          start: 0
        },
        {
          end: 1110,
          frontType: 2,
          start: 760
        },
        {
          end: 1800,
          frontType: 1,
          start: 1550
        }
      ],
      surface: 1,
      turn: 1
    },
    "10204": {
      corners: [
        {
          length: 220,
          start: 520
        },
        {
          length: 220,
          start: 740
        },
        {
          length: 220,
          start: 1310
        },
        {
          length: 220,
          start: 1530
        }
      ],
      course: 1,
      courseSetStatus: [
        1
      ],
      distance: 2e3,
      distanceType: 3,
      finishTimeMax: 123e4,
      finishTimeMin: 1171e3,
      laneMax: 14700,
      raceTrackId: 10002,
      slopes: [
        {
          length: 200,
          slope: -1e4,
          start: 420
        },
        {
          length: 720,
          slope: 1e4,
          start: 865
        }
      ],
      straights: [
        {
          end: 520,
          frontType: 1,
          start: 0
        },
        {
          end: 1310,
          frontType: 2,
          start: 960
        },
        {
          end: 2e3,
          frontType: 1,
          start: 1750
        }
      ],
      surface: 1,
      turn: 1
    },
    "10205": {
      corners: [
        {
          length: 230,
          start: 240
        },
        {
          length: 230,
          start: 470
        },
        {
          length: 230,
          start: 1070
        },
        {
          length: 230,
          start: 1300
        },
        {
          length: 230,
          start: 1890
        },
        {
          length: 230,
          start: 2120
        }
      ],
      course: 1,
      courseSetStatus: [
        2
      ],
      distance: 2600,
      distanceType: 4,
      finishTimeMax: 165e4,
      finishTimeMin: 1576e3,
      laneMax: 14700,
      raceTrackId: 10002,
      slopes: [
        {
          length: 495,
          slope: 1e4,
          start: 0
        },
        {
          length: 200,
          slope: -1e4,
          start: 970
        },
        {
          length: 720,
          slope: 1e4,
          start: 1425
        }
      ],
      straights: [
        {
          end: 240,
          frontType: 2,
          start: 0
        },
        {
          end: 1070,
          frontType: 1,
          start: 700
        },
        {
          end: 1890,
          frontType: 2,
          start: 1530
        },
        {
          end: 2600,
          frontType: 1,
          start: 2350
        }
      ],
      surface: 1,
      turn: 1
    },
    "10206": {
      corners: [
        {
          length: 190,
          start: 370
        },
        {
          length: 190,
          start: 560
        }
      ],
      course: 1,
      courseSetStatus: [],
      distance: 1e3,
      distanceType: 1,
      finishTimeMax: 63e4,
      finishTimeMin: 574e3,
      laneMax: 11e3,
      raceTrackId: 10002,
      slopes: [],
      straights: [
        {
          end: 370,
          frontType: 2,
          start: 0
        },
        {
          end: 1e3,
          frontType: 1,
          start: 750
        }
      ],
      surface: 2,
      turn: 1
    },
    "10207": {
      corners: [
        {
          length: 190,
          start: 350
        },
        {
          length: 190,
          start: 540
        },
        {
          length: 190,
          start: 1070
        },
        {
          length: 190,
          start: 1260
        }
      ],
      course: 1,
      courseSetStatus: [],
      distance: 1700,
      distanceType: 2,
      finishTimeMax: 113e4,
      finishTimeMin: 1014e3,
      laneMax: 11e3,
      raceTrackId: 10002,
      slopes: [
        {
          length: 340,
          slope: -1e4,
          start: 275
        },
        {
          length: 670,
          slope: 1e4,
          start: 615
        }
      ],
      straights: [
        {
          end: 350,
          frontType: 1,
          start: 0
        },
        {
          end: 1070,
          frontType: 2,
          start: 730
        },
        {
          end: 1700,
          frontType: 1,
          start: 1450
        }
      ],
      surface: 2,
      turn: 1
    },
    "10208": {
      corners: [
        {
          length: 190,
          start: 292
        },
        {
          length: 190,
          start: 482
        },
        {
          length: 190,
          start: 1040
        },
        {
          length: 190,
          start: 1230
        },
        {
          length: 190,
          start: 1770
        },
        {
          length: 190,
          start: 1960
        }
      ],
      course: 1,
      courseSetStatus: [
        2
      ],
      distance: 2400,
      distanceType: 3,
      finishTimeMax: 156e4,
      finishTimeMin: 1491e3,
      laneMax: 11e3,
      raceTrackId: 10002,
      slopes: [],
      straights: [
        {
          end: 292,
          frontType: 2,
          start: 0
        },
        {
          end: 1040,
          frontType: 1,
          start: 672
        },
        {
          end: 1770,
          frontType: 2,
          start: 1420
        },
        {
          end: 2400,
          frontType: 1,
          start: 2150
        }
      ],
      surface: 2,
      turn: 1
    },
    "10301": {
      corners: [],
      course: 1,
      courseSetStatus: [
        3
      ],
      distance: 1e3,
      distanceType: 1,
      finishTimeMax: 57e4,
      finishTimeMin: 547e3,
      laneMax: 13500,
      raceTrackId: 10003,
      slopes: [],
      straights: [
        {
          end: 649.900024414062,
          frontType: 1,
          start: 0
        },
        {
          end: 1e3,
          frontType: 1,
          start: 650
        }
      ],
      surface: 1,
      turn: 4
    },
    "10302": {
      corners: [
        {
          length: 200,
          start: 430
        },
        {
          length: 210,
          start: 640
        }
      ],
      course: 2,
      courseSetStatus: [],
      distance: 1200,
      distanceType: 1,
      finishTimeMax: 71e4,
      finishTimeMin: 675e3,
      laneMax: 13500,
      raceTrackId: 10003,
      slopes: [],
      straights: [
        {
          end: 430,
          frontType: 2,
          start: 0
        },
        {
          end: 1200,
          frontType: 1,
          start: 850
        }
      ],
      surface: 1,
      turn: 2
    },
    "10303": {
      corners: [
        {
          length: 200,
          start: 650
        },
        {
          length: 200,
          start: 850
        }
      ],
      course: 2,
      courseSetStatus: [],
      distance: 1400,
      distanceType: 1,
      finishTimeMax: 84e4,
      finishTimeMin: 8e5,
      laneMax: 13500,
      raceTrackId: 10003,
      slopes: [],
      straights: [
        {
          end: 650,
          frontType: 2,
          start: 0
        },
        {
          end: 1400,
          frontType: 1,
          start: 1050
        }
      ],
      surface: 1,
      turn: 2
    },
    "10304": {
      corners: [
        {
          length: 200,
          start: 550
        },
        {
          length: 200,
          start: 750
        }
      ],
      course: 3,
      courseSetStatus: [],
      distance: 1600,
      distanceType: 2,
      finishTimeMax: 95e4,
      finishTimeMin: 908e3,
      laneMax: 13500,
      raceTrackId: 10003,
      slopes: [
        {
          length: 350,
          slope: 1e4,
          start: 250
        },
        {
          length: 300,
          slope: -15e3,
          start: 600
        }
      ],
      straights: [
        {
          end: 550,
          frontType: 2,
          start: 0
        },
        {
          end: 1600,
          frontType: 1,
          start: 950
        }
      ],
      surface: 1,
      turn: 2
    },
    "10305": {
      corners: [
        {
          length: 200,
          start: 750
        },
        {
          length: 200,
          start: 950
        }
      ],
      course: 3,
      courseSetStatus: [
        3
      ],
      distance: 1800,
      distanceType: 2,
      finishTimeMax: 11e5,
      finishTimeMin: 1044e3,
      laneMax: 13500,
      raceTrackId: 10003,
      slopes: [
        {
          length: 350,
          slope: 1e4,
          start: 450
        },
        {
          length: 300,
          slope: -15e3,
          start: 800
        }
      ],
      straights: [
        {
          end: 750,
          frontType: 2,
          start: 0
        },
        {
          end: 1800,
          frontType: 1,
          start: 1150
        }
      ],
      surface: 1,
      turn: 2
    },
    "10306": {
      corners: [
        {
          length: 200,
          start: 420
        },
        {
          length: 200,
          start: 620
        },
        {
          length: 200,
          start: 1250
        },
        {
          length: 200,
          start: 1450
        }
      ],
      course: 2,
      courseSetStatus: [
        2,
        3
      ],
      distance: 2e3,
      distanceType: 3,
      finishTimeMax: 123e4,
      finishTimeMin: 1171e3,
      laneMax: 13500,
      raceTrackId: 10003,
      slopes: [],
      straights: [
        {
          end: 420,
          frontType: 1,
          start: 0
        },
        {
          end: 1250,
          frontType: 2,
          start: 820
        },
        {
          end: 2e3,
          frontType: 1,
          start: 1650
        }
      ],
      surface: 1,
      turn: 2
    },
    "10307": {
      corners: [
        {
          length: 200,
          start: 950
        },
        {
          length: 200,
          start: 1150
        }
      ],
      course: 3,
      courseSetStatus: [
        2,
        3
      ],
      distance: 2e3,
      distanceType: 3,
      finishTimeMax: 123e4,
      finishTimeMin: 1171e3,
      laneMax: 13500,
      raceTrackId: 10003,
      slopes: [
        {
          length: 350,
          slope: 1e4,
          start: 650
        },
        {
          length: 300,
          slope: -15e3,
          start: 1e3
        }
      ],
      straights: [
        {
          end: 950,
          frontType: 2,
          start: 0
        },
        {
          end: 2e3,
          frontType: 1,
          start: 1350
        }
      ],
      surface: 1,
      turn: 2
    },
    "10308": {
      corners: [
        {
          length: 200,
          start: 650
        },
        {
          length: 200,
          start: 850
        },
        {
          length: 200,
          start: 1450
        },
        {
          length: 200,
          start: 1650
        }
      ],
      course: 2,
      courseSetStatus: [
        1
      ],
      distance: 2200,
      distanceType: 3,
      finishTimeMax: 135e4,
      finishTimeMin: 1302e3,
      laneMax: 13500,
      raceTrackId: 10003,
      slopes: [],
      straights: [
        {
          end: 650,
          frontType: 1,
          start: 0
        },
        {
          end: 1450,
          frontType: 2,
          start: 1050
        },
        {
          end: 2200,
          frontType: 1,
          start: 1850
        }
      ],
      surface: 1,
      turn: 2
    },
    "10309": {
      corners: [
        {
          length: 200,
          start: 810
        },
        {
          length: 200,
          start: 1010
        },
        {
          length: 200,
          start: 1650
        },
        {
          length: 200,
          start: 1850
        }
      ],
      course: 2,
      courseSetStatus: [],
      distance: 2400,
      distanceType: 3,
      finishTimeMax: 149e4,
      finishTimeMin: 1416e3,
      laneMax: 13500,
      raceTrackId: 10003,
      slopes: [],
      straights: [
        {
          end: 810,
          frontType: 1,
          start: 0
        },
        {
          end: 1650,
          frontType: 2,
          start: 1210
        },
        {
          end: 2400,
          frontType: 1,
          start: 2050
        }
      ],
      surface: 1,
      turn: 2
    },
    "10310": {
      corners: [
        {
          length: 150,
          start: 600
        },
        {
          length: 150,
          start: 750
        }
      ],
      course: 1,
      courseSetStatus: [],
      distance: 1200,
      distanceType: 1,
      finishTimeMax: 77e4,
      finishTimeMin: 69e4,
      laneMax: 11e3,
      raceTrackId: 10003,
      slopes: [],
      straights: [
        {
          end: 600,
          frontType: 2,
          start: 0
        },
        {
          end: 1200,
          frontType: 1,
          start: 900
        }
      ],
      surface: 2,
      turn: 2
    },
    "10311": {
      corners: [
        {
          length: 160,
          start: 400
        },
        {
          length: 160,
          start: 560
        },
        {
          length: 160,
          start: 1140
        },
        {
          length: 160,
          start: 1300
        }
      ],
      course: 1,
      courseSetStatus: [
        5
      ],
      distance: 1800,
      distanceType: 2,
      finishTimeMax: 118e4,
      finishTimeMin: 1081e3,
      laneMax: 11e3,
      raceTrackId: 10003,
      slopes: [],
      straights: [
        {
          end: 400,
          frontType: 1,
          start: 0
        },
        {
          end: 1140,
          frontType: 2,
          start: 720
        },
        {
          end: 1800,
          frontType: 1,
          start: 1460
        }
      ],
      surface: 2,
      turn: 2
    },
    "10312": {
      corners: [
        {
          length: 160,
          start: 380
        },
        {
          length: 160,
          start: 540
        },
        {
          length: 160,
          start: 1120
        },
        {
          length: 160,
          start: 1280
        },
        {
          length: 160,
          start: 1850
        },
        {
          length: 160,
          start: 2010
        }
      ],
      course: 1,
      courseSetStatus: [],
      distance: 2500,
      distanceType: 4,
      finishTimeMax: 164e4,
      finishTimeMin: 1591e3,
      laneMax: 11e3,
      raceTrackId: 10003,
      slopes: [],
      straights: [
        {
          end: 380,
          frontType: 2,
          start: 0
        },
        {
          end: 1120,
          frontType: 1,
          start: 700
        },
        {
          end: 1850,
          frontType: 2,
          start: 1440
        },
        {
          end: 2500,
          frontType: 1,
          start: 2170
        }
      ],
      surface: 2,
      turn: 2
    },
    "10401": {
      corners: [
        {
          length: 300,
          start: 420
        },
        {
          length: 200,
          start: 720
        }
      ],
      course: 1,
      courseSetStatus: [],
      distance: 1200,
      distanceType: 1,
      finishTimeMax: 71e4,
      finishTimeMin: 675e3,
      laneMax: 13e3,
      raceTrackId: 10004,
      slopes: [
        {
          length: 100,
          slope: 15e3,
          start: 180
        }
      ],
      straights: [
        {
          end: 420,
          frontType: 2,
          start: 0
        },
        {
          end: 1200,
          frontType: 1,
          start: 920
        }
      ],
      surface: 1,
      turn: 1
    },
    "10402": {
      corners: [
        {
          length: 200,
          start: 330
        },
        {
          length: 200,
          start: 530
        },
        {
          length: 300,
          start: 1020
        },
        {
          length: 200,
          start: 1320
        }
      ],
      course: 1,
      courseSetStatus: [
        2
      ],
      distance: 1800,
      distanceType: 2,
      finishTimeMax: 11e5,
      finishTimeMin: 1044e3,
      laneMax: 13e3,
      raceTrackId: 10004,
      slopes: [
        {
          length: 100,
          slope: 15e3,
          start: 780
        }
      ],
      straights: [
        {
          end: 330,
          frontType: 1,
          start: 0
        },
        {
          end: 1020,
          frontType: 2,
          start: 730
        },
        {
          end: 1800,
          frontType: 1,
          start: 1520
        }
      ],
      surface: 1,
      turn: 1
    },
    "10403": {
      corners: [
        {
          length: 200,
          start: 530
        },
        {
          length: 200,
          start: 730
        },
        {
          length: 300,
          start: 1220
        },
        {
          length: 200,
          start: 1520
        }
      ],
      course: 1,
      courseSetStatus: [
        2
      ],
      distance: 2e3,
      distanceType: 3,
      finishTimeMax: 123e4,
      finishTimeMin: 1171e3,
      laneMax: 13e3,
      raceTrackId: 10004,
      slopes: [
        {
          length: 100,
          slope: 15e3,
          start: 980
        }
      ],
      straights: [
        {
          end: 530,
          frontType: 1,
          start: 0
        },
        {
          end: 1220,
          frontType: 2,
          start: 930
        },
        {
          end: 2e3,
          frontType: 1,
          start: 1720
        }
      ],
      surface: 1,
      turn: 1
    },
    "10404": {
      corners: [
        {
          length: 300,
          start: 220
        },
        {
          length: 200,
          start: 520
        },
        {
          length: 200,
          start: 1130
        },
        {
          length: 200,
          start: 1330
        },
        {
          length: 300,
          start: 1820
        },
        {
          length: 200,
          start: 2120
        }
      ],
      course: 1,
      courseSetStatus: [],
      distance: 2600,
      distanceType: 4,
      finishTimeMax: 165e4,
      finishTimeMin: 1576e3,
      laneMax: 13e3,
      raceTrackId: 10004,
      slopes: [
        {
          length: 80,
          slope: 15e3,
          start: 0
        },
        {
          length: 100,
          slope: 15e3,
          start: 1580
        }
      ],
      straights: [
        {
          end: 220,
          frontType: 2,
          start: 0
        },
        {
          end: 1130,
          frontType: 1,
          start: 720
        },
        {
          end: 1820,
          frontType: 2,
          start: 1530
        },
        {
          end: 2600,
          frontType: 1,
          start: 2320
        }
      ],
      surface: 1,
      turn: 1
    },
    "10405": {
      corners: [
        {
          length: 210,
          start: 500
        },
        {
          length: 160,
          start: 710
        }
      ],
      course: 1,
      courseSetStatus: [],
      distance: 1150,
      distanceType: 1,
      finishTimeMax: 72e4,
      finishTimeMin: 666e3,
      laneMax: 11500,
      raceTrackId: 10004,
      slopes: [],
      straights: [
        {
          end: 500,
          frontType: 2,
          start: 0
        },
        {
          end: 1150,
          frontType: 1,
          start: 870
        }
      ],
      surface: 2,
      turn: 1
    },
    "10406": {
      corners: [
        {
          length: 170,
          start: 360
        },
        {
          length: 170,
          start: 530
        },
        {
          length: 210,
          start: 1050
        },
        {
          length: 160,
          start: 1260
        }
      ],
      course: 1,
      courseSetStatus: [
        3
      ],
      distance: 1700,
      distanceType: 2,
      finishTimeMax: 113e4,
      finishTimeMin: 1014e3,
      laneMax: 11500,
      raceTrackId: 10004,
      slopes: [
        {
          length: 320,
          slope: -1e4,
          start: 285
        }
      ],
      straights: [
        {
          end: 360,
          frontType: 1,
          start: 0
        },
        {
          end: 1050,
          frontType: 2,
          start: 700
        },
        {
          end: 1700,
          frontType: 1,
          start: 1420
        }
      ],
      surface: 2,
      turn: 1
    },
    "10407": {
      corners: [
        {
          length: 210,
          start: 310
        },
        {
          length: 160,
          start: 520
        },
        {
          length: 170,
          start: 1060
        },
        {
          length: 170,
          start: 1230
        },
        {
          length: 210,
          start: 1750
        },
        {
          length: 160,
          start: 1960
        }
      ],
      course: 1,
      courseSetStatus: [
        2
      ],
      distance: 2400,
      distanceType: 3,
      finishTimeMax: 156e4,
      finishTimeMin: 1491e3,
      laneMax: 11500,
      raceTrackId: 10004,
      slopes: [],
      straights: [
        {
          end: 310,
          frontType: 2,
          start: 0
        },
        {
          end: 1060,
          frontType: 1,
          start: 680
        },
        {
          end: 1750,
          frontType: 2,
          start: 1400
        },
        {
          end: 2400,
          frontType: 1,
          start: 2120
        }
      ],
      surface: 2,
      turn: 1
    },
    "10501": {
      corners: [
        {
          length: 350,
          start: 300
        },
        {
          length: 250,
          start: 650
        }
      ],
      course: 3,
      courseSetStatus: [],
      distance: 1200,
      distanceType: 1,
      finishTimeMax: 71e4,
      finishTimeMin: 675e3,
      laneMax: 12e3,
      raceTrackId: 10005,
      slopes: [
        {
          length: 200,
          slope: -15e3,
          start: 0
        },
        {
          length: 110,
          slope: 2e4,
          start: 1025
        }
      ],
      straights: [
        {
          end: 1200,
          frontType: 1,
          start: 900
        }
      ],
      surface: 1,
      turn: 1
    },
    "10502": {
      corners: [
        {
          length: 450,
          start: 50
        },
        {
          length: 350,
          start: 700
        },
        {
          length: 250,
          start: 1050
        }
      ],
      course: 3,
      courseSetStatus: [
        3
      ],
      distance: 1600,
      distanceType: 2,
      finishTimeMax: 95e4,
      finishTimeMin: 908e3,
      laneMax: 12e3,
      raceTrackId: 10005,
      slopes: [
        {
          length: 300,
          slope: -15e3,
          start: 300
        },
        {
          length: 110,
          slope: 2e4,
          start: 1425
        }
      ],
      straights: [
        {
          end: 1600,
          frontType: 1,
          start: 1300
        }
      ],
      surface: 1,
      turn: 1
    },
    "10503": {
      corners: [
        {
          length: 250,
          start: 175
        },
        {
          length: 250,
          start: 425
        },
        {
          length: 250,
          start: 1e3
        },
        {
          length: 250,
          start: 1250
        }
      ],
      course: 2,
      courseSetStatus: [],
      distance: 1800,
      distanceType: 2,
      finishTimeMax: 11e5,
      finishTimeMin: 1044e3,
      laneMax: 12e3,
      raceTrackId: 10005,
      slopes: [
        {
          length: 35,
          slope: 2e4,
          start: 1
        },
        {
          length: 200,
          slope: 15e3,
          start: 125
        },
        {
          length: 400,
          slope: -15e3,
          start: 425
        },
        {
          length: 110,
          slope: 2e4,
          start: 1625
        }
      ],
      straights: [
        {
          end: 175,
          frontType: 1,
          start: 0
        },
        {
          end: 1e3,
          frontType: 2,
          start: 675
        },
        {
          end: 1800,
          frontType: 1,
          start: 1500
        }
      ],
      surface: 1,
      turn: 1
    },
    "10504": {
      corners: [
        {
          length: 250,
          start: 375
        },
        {
          length: 250,
          start: 625
        },
        {
          length: 250,
          start: 1200
        },
        {
          length: 250,
          start: 1450
        }
      ],
      course: 2,
      courseSetStatus: [
        1
      ],
      distance: 2e3,
      distanceType: 3,
      finishTimeMax: 123e4,
      finishTimeMin: 1171e3,
      laneMax: 12e3,
      raceTrackId: 10005,
      slopes: [
        {
          length: 110,
          slope: 2e4,
          start: 125
        },
        {
          length: 200,
          slope: 15e3,
          start: 325
        },
        {
          length: 400,
          slope: -15e3,
          start: 625
        },
        {
          length: 110,
          slope: 2e4,
          start: 1825
        }
      ],
      straights: [
        {
          end: 375,
          frontType: 1,
          start: 0
        },
        {
          end: 1200,
          frontType: 2,
          start: 875
        },
        {
          end: 2e3,
          frontType: 1,
          start: 1700
        }
      ],
      surface: 1,
      turn: 1
    },
    "10505": {
      corners: [
        {
          length: 247,
          start: 403
        },
        {
          length: 450,
          start: 650
        },
        {
          length: 350,
          start: 1300
        },
        {
          length: 250,
          start: 1650
        }
      ],
      course: 3,
      courseSetStatus: [
        2,
        4
      ],
      distance: 2200,
      distanceType: 3,
      finishTimeMax: 135e4,
      finishTimeMin: 1302e3,
      laneMax: 12e3,
      raceTrackId: 10005,
      slopes: [
        {
          length: 110,
          slope: 2e4,
          start: 153
        },
        {
          length: 200,
          slope: 15e3,
          start: 353
        },
        {
          length: 300,
          slope: -15e3,
          start: 900
        },
        {
          length: 110,
          slope: 2e4,
          start: 2025
        }
      ],
      straights: [
        {
          end: 403,
          frontType: 1,
          start: 0
        },
        {
          end: 2200,
          frontType: 1,
          start: 1900
        }
      ],
      surface: 1,
      turn: 1
    },
    "10506": {
      corners: [
        {
          length: 146,
          start: 100
        },
        {
          length: 250,
          start: 246
        },
        {
          length: 250,
          start: 875
        },
        {
          length: 250,
          start: 1125
        },
        {
          length: 250,
          start: 1700
        },
        {
          length: 250,
          start: 1950
        }
      ],
      course: 2,
      courseSetStatus: [
        2,
        4
      ],
      distance: 2500,
      distanceType: 4,
      finishTimeMax: 157e4,
      finishTimeMin: 1487e3,
      laneMax: 12e3,
      raceTrackId: 10005,
      slopes: [
        {
          length: 110,
          slope: 2e4,
          start: 621
        },
        {
          length: 200,
          slope: 15e3,
          start: 825
        },
        {
          length: 400,
          slope: -15e3,
          start: 1125
        },
        {
          length: 110,
          slope: 2e4,
          start: 2325
        }
      ],
      straights: [
        {
          end: 875,
          frontType: 1,
          start: 496
        },
        {
          end: 1700,
          frontType: 2,
          start: 1375
        },
        {
          end: 2500,
          frontType: 1,
          start: 2200
        }
      ],
      surface: 1,
      turn: 1
    },
    "10507": {
      corners: [
        {
          length: 250,
          start: 290
        },
        {
          length: 250,
          start: 540
        },
        {
          length: 250,
          start: 1115
        },
        {
          length: 250,
          start: 1365
        },
        {
          length: 250,
          start: 1975
        },
        {
          length: 250,
          start: 2225
        },
        {
          length: 250,
          start: 2800
        },
        {
          length: 250,
          start: 3050
        }
      ],
      course: 2,
      courseSetStatus: [
        2
      ],
      distance: 3600,
      distanceType: 4,
      finishTimeMax: 227e4,
      finishTimeMin: 2231e3,
      laneMax: 12e3,
      raceTrackId: 10005,
      slopes: [
        {
          length: 110,
          slope: 2e4,
          start: 40
        },
        {
          length: 200,
          slope: 15e3,
          start: 240
        },
        {
          length: 400,
          slope: -15e3,
          start: 540
        },
        {
          length: 110,
          slope: 2e4,
          start: 1740
        },
        {
          length: 200,
          slope: 15e3,
          start: 1925
        },
        {
          length: 400,
          slope: -15e3,
          start: 2225
        },
        {
          length: 110,
          slope: 2e4,
          start: 3425
        }
      ],
      straights: [
        {
          end: 290,
          frontType: 1,
          start: 0
        },
        {
          end: 1115,
          frontType: 2,
          start: 790
        },
        {
          end: 1975,
          frontType: 1,
          start: 1615
        },
        {
          end: 2800,
          frontType: 2,
          start: 2475
        },
        {
          end: 3600,
          frontType: 1,
          start: 3300
        }
      ],
      surface: 1,
      turn: 1
    },
    "10508": {
      corners: [
        {
          length: 200,
          start: 500
        },
        {
          length: 200,
          start: 700
        }
      ],
      course: 1,
      courseSetStatus: [
        3
      ],
      distance: 1200,
      distanceType: 1,
      finishTimeMax: 77e4,
      finishTimeMin: 69e4,
      laneMax: 12e3,
      raceTrackId: 10005,
      slopes: [
        {
          length: 175,
          slope: -15e3,
          start: 175
        },
        {
          length: 175,
          slope: 15e3,
          start: 1e3
        }
      ],
      straights: [
        {
          end: 500,
          frontType: 2,
          start: 0
        },
        {
          end: 1200,
          frontType: 1,
          start: 900
        }
      ],
      surface: 2,
      turn: 1
    },
    "10509": {
      corners: [
        {
          length: 200,
          start: 350
        },
        {
          length: 200,
          start: 550
        },
        {
          length: 200,
          start: 1100
        },
        {
          length: 200,
          start: 1300
        }
      ],
      course: 1,
      courseSetStatus: [
        3
      ],
      distance: 1800,
      distanceType: 2,
      finishTimeMax: 118e4,
      finishTimeMin: 1081e3,
      laneMax: 12e3,
      raceTrackId: 10005,
      slopes: [
        {
          length: 175,
          slope: 15e3,
          start: 100
        },
        {
          length: 175,
          slope: 1e4,
          start: 350
        },
        {
          length: 175,
          slope: -15e3,
          start: 775
        },
        {
          length: 175,
          slope: 15e3,
          start: 1600
        }
      ],
      straights: [
        {
          end: 350,
          frontType: 1,
          start: 0
        },
        {
          end: 1100,
          frontType: 1,
          start: 750
        },
        {
          end: 1800,
          frontType: 1,
          start: 1500
        }
      ],
      surface: 2,
      turn: 1
    },
    "10510": {
      corners: [
        {
          length: 200,
          start: 200
        },
        {
          length: 200,
          start: 400
        },
        {
          length: 200,
          start: 950
        },
        {
          length: 200,
          start: 1150
        },
        {
          length: 200,
          start: 1700
        },
        {
          length: 200,
          start: 1900
        }
      ],
      course: 1,
      courseSetStatus: [
        2
      ],
      distance: 2400,
      distanceType: 3,
      finishTimeMax: 156e4,
      finishTimeMin: 1491e3,
      laneMax: 12e3,
      raceTrackId: 10005,
      slopes: [],
      straights: [
        {
          end: 200,
          frontType: 2,
          start: 0
        },
        {
          end: 950,
          frontType: 1,
          start: 600
        },
        {
          end: 1700,
          frontType: 2,
          start: 1350
        },
        {
          end: 2400,
          frontType: 1,
          start: 2100
        }
      ],
      surface: 2,
      turn: 1
    },
    "10511": {
      corners: [
        {
          length: 200,
          start: 300
        },
        {
          length: 200,
          start: 500
        },
        {
          length: 200,
          start: 1050
        },
        {
          length: 200,
          start: 1250
        },
        {
          length: 200,
          start: 1800
        },
        {
          length: 200,
          start: 2e3
        }
      ],
      course: 1,
      courseSetStatus: [],
      distance: 2500,
      distanceType: 4,
      finishTimeMax: 164e4,
      finishTimeMin: 1591e3,
      laneMax: 12e3,
      raceTrackId: 10005,
      slopes: [],
      straights: [
        {
          end: 300,
          frontType: 2,
          start: 0
        },
        {
          end: 1050,
          frontType: 1,
          start: 700
        },
        {
          end: 1800,
          frontType: 2,
          start: 1450
        },
        {
          end: 2500,
          frontType: 1,
          start: 2200
        }
      ],
      surface: 2,
      turn: 1
    },
    "10601": {
      corners: [
        {
          length: 275,
          start: 350
        },
        {
          length: 275,
          start: 625
        }
      ],
      course: 1,
      courseSetStatus: [
        2,
        3
      ],
      distance: 1400,
      distanceType: 1,
      finishTimeMax: 84e4,
      finishTimeMin: 8e5,
      laneMax: 15e3,
      raceTrackId: 10006,
      slopes: [
        {
          length: 75,
          slope: 2e4,
          start: 125
        },
        {
          length: 250,
          slope: -15e3,
          start: 250
        },
        {
          length: 150,
          slope: 15e3,
          start: 950
        }
      ],
      straights: [
        {
          end: 350,
          frontType: 2,
          start: 0
        },
        {
          end: 1400,
          frontType: 1,
          start: 900
        }
      ],
      surface: 1,
      turn: 2
    },
    "10602": {
      corners: [
        {
          length: 275,
          start: 550
        },
        {
          length: 275,
          start: 825
        }
      ],
      course: 1,
      courseSetStatus: [
        2,
        4
      ],
      distance: 1600,
      distanceType: 2,
      finishTimeMax: 95e4,
      finishTimeMin: 908e3,
      laneMax: 15e3,
      raceTrackId: 10006,
      slopes: [
        {
          length: 75,
          slope: 2e4,
          start: 325
        },
        {
          length: 250,
          slope: -15e3,
          start: 450
        },
        {
          length: 150,
          slope: 15e3,
          start: 1150
        }
      ],
      straights: [
        {
          end: 550,
          frontType: 2,
          start: 0
        },
        {
          end: 1600,
          frontType: 1,
          start: 1100
        }
      ],
      surface: 1,
      turn: 2
    },
    "10603": {
      corners: [
        {
          length: 250,
          start: 75
        },
        {
          length: 275,
          start: 750
        },
        {
          length: 275,
          start: 1025
        }
      ],
      course: 1,
      courseSetStatus: [
        1
      ],
      distance: 1800,
      distanceType: 2,
      finishTimeMax: 11e5,
      finishTimeMin: 1044e3,
      laneMax: 15e3,
      raceTrackId: 10006,
      slopes: [
        {
          length: 75,
          slope: 2e4,
          start: 525
        },
        {
          length: 250,
          slope: -15e3,
          start: 650
        },
        {
          length: 150,
          slope: 15e3,
          start: 1350
        }
      ],
      straights: [
        {
          end: 750,
          frontType: 2,
          start: 325
        },
        {
          end: 1800,
          frontType: 1,
          start: 1300
        }
      ],
      surface: 1,
      turn: 2
    },
    "10604": {
      corners: [
        {
          length: 200,
          start: 200
        },
        {
          length: 275,
          start: 950
        },
        {
          length: 275,
          start: 1225
        }
      ],
      course: 1,
      courseSetStatus: [],
      distance: 2e3,
      distanceType: 3,
      finishTimeMax: 123e4,
      finishTimeMin: 1171e3,
      laneMax: 15e3,
      raceTrackId: 10006,
      slopes: [
        {
          length: 75,
          slope: 2e4,
          start: 725
        },
        {
          length: 250,
          slope: -15e3,
          start: 850
        },
        {
          length: 150,
          slope: 15e3,
          start: 1550
        }
      ],
      straights: [
        {
          end: 950,
          frontType: 2,
          start: 400
        },
        {
          end: 2e3,
          frontType: 1,
          start: 1500
        }
      ],
      surface: 1,
      turn: 2
    },
    "10605": {
      corners: [
        {
          length: 250,
          start: 225
        },
        {
          length: 325,
          start: 475
        },
        {
          length: 275,
          start: 1250
        },
        {
          length: 275,
          start: 1525
        }
      ],
      course: 1,
      courseSetStatus: [
        3
      ],
      distance: 2300,
      distanceType: 3,
      finishTimeMax: 143e4,
      finishTimeMin: 1393e3,
      laneMax: 15e3,
      raceTrackId: 10006,
      slopes: [
        {
          length: 75,
          slope: 2e4,
          start: 1025
        },
        {
          length: 250,
          slope: -15e3,
          start: 1150
        },
        {
          length: 150,
          slope: 15e3,
          start: 1850
        }
      ],
      straights: [
        {
          end: 225,
          frontType: 1,
          start: 0
        },
        {
          end: 1250,
          frontType: 2,
          start: 800
        },
        {
          end: 2300,
          frontType: 1,
          start: 1800
        }
      ],
      surface: 1,
      turn: 2
    },
    "10606": {
      corners: [
        {
          length: 250,
          start: 325
        },
        {
          length: 325,
          start: 575
        },
        {
          length: 275,
          start: 1350
        },
        {
          length: 275,
          start: 1625
        }
      ],
      course: 1,
      courseSetStatus: [],
      distance: 2400,
      distanceType: 3,
      finishTimeMax: 149e4,
      finishTimeMin: 1416e3,
      laneMax: 15e3,
      raceTrackId: 10006,
      slopes: [
        {
          length: 40,
          slope: 15e3,
          start: 0
        },
        {
          length: 75,
          slope: 2e4,
          start: 1125
        },
        {
          length: 250,
          slope: -15e3,
          start: 1250
        },
        {
          length: 150,
          slope: 15e3,
          start: 1950
        }
      ],
      straights: [
        {
          end: 325,
          frontType: 1,
          start: 0
        },
        {
          end: 1350,
          frontType: 2,
          start: 900
        },
        {
          end: 2400,
          frontType: 1,
          start: 1900
        }
      ],
      surface: 1,
      turn: 2
    },
    "10607": {
      corners: [
        {
          length: 250,
          start: 425
        },
        {
          length: 325,
          start: 675
        },
        {
          length: 275,
          start: 1450
        },
        {
          length: 275,
          start: 1725
        }
      ],
      course: 1,
      courseSetStatus: [
        2
      ],
      distance: 2500,
      distanceType: 4,
      finishTimeMax: 157e4,
      finishTimeMin: 1487e3,
      laneMax: 15e3,
      raceTrackId: 10006,
      slopes: [
        {
          length: 140,
          slope: 15e3,
          start: 0
        },
        {
          length: 75,
          slope: 2e4,
          start: 1225
        },
        {
          length: 250,
          slope: -15e3,
          start: 1350
        },
        {
          length: 150,
          slope: 15e3,
          start: 2050
        }
      ],
      straights: [
        {
          end: 425,
          frontType: 1,
          start: 0
        },
        {
          end: 1450,
          frontType: 2,
          start: 1e3
        },
        {
          end: 2500,
          frontType: 1,
          start: 2e3
        }
      ],
      surface: 1,
      turn: 2
    },
    "10608": {
      corners: [
        {
          length: 275,
          start: 292
        },
        {
          length: 275,
          start: 567
        },
        {
          length: 250,
          start: 1325
        },
        {
          length: 325,
          start: 1575
        },
        {
          length: 275,
          start: 2350
        },
        {
          length: 275,
          start: 2625
        }
      ],
      course: 1,
      courseSetStatus: [],
      distance: 3400,
      distanceType: 4,
      finishTimeMax: 214e4,
      finishTimeMin: 2099e3,
      laneMax: 15e3,
      raceTrackId: 10006,
      slopes: [
        {
          length: 75,
          slope: 2e4,
          start: 67
        },
        {
          length: 250,
          slope: -15e3,
          start: 192
        },
        {
          length: 150,
          slope: 15e3,
          start: 892
        },
        {
          length: 75,
          slope: 2e4,
          start: 2125
        },
        {
          length: 250,
          slope: -15e3,
          start: 2250
        },
        {
          length: 150,
          slope: 15e3,
          start: 2950
        }
      ],
      straights: [
        {
          end: 292,
          frontType: 2,
          start: 0
        },
        {
          end: 1325,
          frontType: 1,
          start: 842
        },
        {
          end: 2350,
          frontType: 2,
          start: 1900
        },
        {
          end: 3400,
          frontType: 1,
          start: 2900
        }
      ],
      surface: 1,
      turn: 2
    },
    "10609": {
      corners: [
        {
          length: 225,
          start: 350
        },
        {
          length: 225,
          start: 575
        }
      ],
      course: 1,
      courseSetStatus: [
        1
      ],
      distance: 1300,
      distanceType: 1,
      finishTimeMax: 82e4,
      finishTimeMin: 766e3,
      laneMax: 12500,
      raceTrackId: 10006,
      slopes: [
        {
          length: 200,
          slope: -1e4,
          start: 275
        },
        {
          length: 250,
          slope: 15e3,
          start: 800
        }
      ],
      straights: [
        {
          end: 350,
          frontType: 2,
          start: 0
        },
        {
          end: 1300,
          frontType: 1,
          start: 800
        }
      ],
      surface: 2,
      turn: 2
    },
    "10610": {
      corners: [
        {
          length: 225,
          start: 450
        },
        {
          length: 225,
          start: 675
        }
      ],
      course: 1,
      courseSetStatus: [
        2
      ],
      distance: 1400,
      distanceType: 1,
      finishTimeMax: 94e4,
      finishTimeMin: 808e3,
      laneMax: 12500,
      raceTrackId: 10006,
      slopes: [
        {
          length: 200,
          slope: -1e4,
          start: 375
        },
        {
          length: 250,
          slope: 15e3,
          start: 900
        }
      ],
      straights: [
        {
          end: 450,
          frontType: 2,
          start: 0
        },
        {
          end: 1400,
          frontType: 1,
          start: 900
        }
      ],
      surface: 2,
      turn: 2
    },
    "10611": {
      corners: [
        {
          length: 225,
          start: 650
        },
        {
          length: 225,
          start: 875
        }
      ],
      course: 1,
      courseSetStatus: [
        1,
        2
      ],
      distance: 1600,
      distanceType: 2,
      finishTimeMax: 108e4,
      finishTimeMin: 941e3,
      laneMax: 12500,
      raceTrackId: 10006,
      slopes: [
        {
          length: 200,
          slope: -1e4,
          start: 575
        },
        {
          length: 250,
          slope: 15e3,
          start: 1100
        }
      ],
      straights: [
        {
          end: 650,
          frontType: 2,
          start: 0
        },
        {
          end: 1600,
          frontType: 1,
          start: 1100
        }
      ],
      surface: 2,
      turn: 2
    },
    "10612": {
      corners: [
        {
          length: 250,
          start: 200
        },
        {
          length: 250,
          start: 450
        },
        {
          length: 225,
          start: 1150
        },
        {
          length: 225,
          start: 1375
        }
      ],
      course: 1,
      courseSetStatus: [],
      distance: 2100,
      distanceType: 3,
      finishTimeMax: 133e4,
      finishTimeMin: 1274e3,
      laneMax: 12500,
      raceTrackId: 10006,
      slopes: [
        {
          length: 200,
          slope: -1e4,
          start: 1075
        },
        {
          length: 250,
          slope: 15e3,
          start: 1600
        }
      ],
      straights: [
        {
          end: 200,
          frontType: 1,
          start: 0
        },
        {
          end: 1150,
          frontType: 2,
          start: 700
        },
        {
          end: 2100,
          frontType: 1,
          start: 1600
        }
      ],
      surface: 2,
      turn: 2
    },
    "10613": {
      corners: [
        {
          length: 250,
          start: 500
        },
        {
          length: 250,
          start: 750
        },
        {
          length: 225,
          start: 1450
        },
        {
          length: 225,
          start: 1675
        },
        {
          length: 500,
          start: 2348
        }
      ],
      course: 1,
      courseSetStatus: [
        2
      ],
      distance: 2400,
      distanceType: 3,
      finishTimeMax: 156e4,
      finishTimeMin: 1491e3,
      laneMax: 12500,
      raceTrackId: 10006,
      slopes: [],
      straights: [
        {
          end: 500,
          frontType: 1,
          start: 0
        },
        {
          end: 1450,
          frontType: 2,
          start: 1e3
        },
        {
          end: 2400,
          frontType: 1,
          start: 1900
        }
      ],
      surface: 2,
      turn: 2
    },
    "10701": {
      corners: [
        {
          length: 250,
          start: 300
        },
        {
          length: 250,
          start: 550
        }
      ],
      course: 1,
      courseSetStatus: [],
      distance: 1200,
      distanceType: 1,
      finishTimeMax: 71e4,
      finishTimeMin: 675e3,
      laneMax: 14400,
      raceTrackId: 10007,
      slopes: [
        {
          length: 775,
          slope: -1e4,
          start: 100
        },
        {
          length: 100,
          slope: 2e4,
          start: 875
        }
      ],
      straights: [
        {
          end: 300,
          frontType: 2,
          start: 0
        },
        {
          end: 1200,
          frontType: 1,
          start: 800
        }
      ],
      surface: 1,
      turn: 2
    },
    "10702": {
      corners: [
        {
          length: 250,
          start: 500
        },
        {
          length: 250,
          start: 750
        }
      ],
      course: 1,
      courseSetStatus: [],
      distance: 1400,
      distanceType: 1,
      finishTimeMax: 84e4,
      finishTimeMin: 8e5,
      laneMax: 14400,
      raceTrackId: 10007,
      slopes: [
        {
          length: 775,
          slope: -1e4,
          start: 300
        },
        {
          length: 100,
          slope: 2e4,
          start: 1075
        }
      ],
      straights: [
        {
          end: 500,
          frontType: 2,
          start: 0
        },
        {
          end: 1400,
          frontType: 1,
          start: 1e3
        }
      ],
      surface: 1,
      turn: 2
    },
    "10703": {
      corners: [
        {
          length: 150,
          start: 150
        },
        {
          length: 250,
          start: 700
        },
        {
          length: 250,
          start: 950
        }
      ],
      course: 1,
      courseSetStatus: [
        1
      ],
      distance: 1600,
      distanceType: 2,
      finishTimeMax: 95e4,
      finishTimeMin: 908e3,
      laneMax: 14400,
      raceTrackId: 10007,
      slopes: [
        {
          length: 775,
          slope: -1e4,
          start: 500
        },
        {
          length: 100,
          slope: 2e4,
          start: 1275
        }
      ],
      straights: [
        {
          end: 700,
          frontType: 2,
          start: 300
        },
        {
          end: 1600,
          frontType: 1,
          start: 1200
        }
      ],
      surface: 1,
      turn: 2
    },
    "10704": {
      corners: [
        {
          length: 200,
          start: 300
        },
        {
          length: 200,
          start: 500
        },
        {
          length: 250,
          start: 1100
        },
        {
          length: 250,
          start: 1350
        }
      ],
      course: 1,
      courseSetStatus: [],
      distance: 2e3,
      distanceType: 3,
      finishTimeMax: 123e4,
      finishTimeMin: 1171e3,
      laneMax: 14400,
      raceTrackId: 10007,
      slopes: [
        {
          length: 50,
          slope: 2e4,
          start: 0
        },
        {
          length: 775,
          slope: -1e4,
          start: 900
        },
        {
          length: 100,
          slope: 2e4,
          start: 1675
        }
      ],
      straights: [
        {
          end: 300,
          frontType: 1,
          start: 0
        },
        {
          end: 1100,
          frontType: 2,
          start: 700
        },
        {
          end: 2e3,
          frontType: 1,
          start: 1600
        }
      ],
      surface: 1,
      turn: 2
    },
    "10705": {
      corners: [
        {
          length: 200,
          start: 500
        },
        {
          length: 200,
          start: 700
        },
        {
          length: 250,
          start: 1300
        },
        {
          length: 250,
          start: 1550
        }
      ],
      course: 1,
      courseSetStatus: [
        2
      ],
      distance: 2200,
      distanceType: 3,
      finishTimeMax: 135e4,
      finishTimeMin: 1302e3,
      laneMax: 14400,
      raceTrackId: 10007,
      slopes: [
        {
          length: 150,
          slope: -1e4,
          start: 0
        },
        {
          length: 100,
          slope: 2e4,
          start: 150
        },
        {
          length: 775,
          slope: -1e4,
          start: 1100
        },
        {
          length: 100,
          slope: 2e4,
          start: 1875
        }
      ],
      straights: [
        {
          end: 500,
          frontType: 1,
          start: 0
        },
        {
          end: 1300,
          frontType: 2,
          start: 900
        },
        {
          end: 2200,
          frontType: 1,
          start: 1800
        }
      ],
      surface: 1,
      turn: 2
    },
    "10706": {
      corners: [
        {
          length: 200,
          start: 400
        },
        {
          length: 200,
          start: 600
        }
      ],
      course: 1,
      courseSetStatus: [],
      distance: 1200,
      distanceType: 1,
      finishTimeMax: 77e4,
      finishTimeMin: 69e4,
      laneMax: 13e3,
      raceTrackId: 10007,
      slopes: [],
      straights: [
        {
          end: 400,
          frontType: 2,
          start: 0
        },
        {
          end: 1200,
          frontType: 1,
          start: 800
        }
      ],
      surface: 2,
      turn: 2
    },
    "10707": {
      corners: [
        {
          length: 200,
          start: 600
        },
        {
          length: 200,
          start: 800
        }
      ],
      course: 1,
      courseSetStatus: [],
      distance: 1400,
      distanceType: 1,
      finishTimeMax: 94e4,
      finishTimeMin: 808e3,
      laneMax: 13e3,
      raceTrackId: 10007,
      slopes: [
        {
          length: 600,
          slope: -15e3,
          start: 425
        },
        {
          length: 150,
          slope: 15e3,
          start: 1025
        }
      ],
      straights: [
        {
          end: 600,
          frontType: 2,
          start: 0
        },
        {
          end: 1400,
          frontType: 1,
          start: 1e3
        }
      ],
      surface: 2,
      turn: 2
    },
    "10708": {
      corners: [
        {
          length: 165,
          start: 270
        },
        {
          length: 165,
          start: 435
        },
        {
          length: 200,
          start: 1e3
        },
        {
          length: 200,
          start: 1200
        }
      ],
      course: 1,
      courseSetStatus: [
        2
      ],
      distance: 1800,
      distanceType: 2,
      finishTimeMax: 118e4,
      finishTimeMin: 1081e3,
      laneMax: 13e3,
      raceTrackId: 10007,
      slopes: [
        {
          length: 50,
          slope: 15e3,
          start: 0
        },
        {
          length: 600,
          slope: -15e3,
          start: 825
        },
        {
          length: 150,
          slope: 15e3,
          start: 1425
        }
      ],
      straights: [
        {
          end: 270,
          frontType: 1,
          start: 0
        },
        {
          end: 1e3,
          frontType: 2,
          start: 600
        },
        {
          end: 1800,
          frontType: 1,
          start: 1400
        }
      ],
      surface: 2,
      turn: 2
    },
    "10709": {
      corners: [
        {
          length: 165,
          start: 370
        },
        {
          length: 165,
          start: 535
        },
        {
          length: 200,
          start: 1100
        },
        {
          length: 200,
          start: 1300
        }
      ],
      course: 1,
      courseSetStatus: [],
      distance: 1900,
      distanceType: 3,
      finishTimeMax: 123e4,
      finishTimeMin: 1142e3,
      laneMax: 13e3,
      raceTrackId: 10007,
      slopes: [],
      straights: [
        {
          end: 370,
          frontType: 1,
          start: 0
        },
        {
          end: 1100,
          frontType: 2,
          start: 700
        },
        {
          end: 1900,
          frontType: 1,
          start: 1500
        }
      ],
      surface: 2,
      turn: 2
    },
    "10801": {
      corners: [
        {
          length: 275,
          start: 320
        },
        {
          length: 275,
          start: 595
        }
      ],
      course: 2,
      courseSetStatus: [],
      distance: 1200,
      distanceType: 1,
      finishTimeMax: 71e4,
      finishTimeMin: 675e3,
      laneMax: 14100,
      raceTrackId: 10008,
      slopes: [
        {
          length: 175,
          slope: 15e3,
          start: 120
        },
        {
          length: 150,
          slope: -15e3,
          start: 420
        }
      ],
      straights: [
        {
          end: 320,
          frontType: 2,
          start: 0
        },
        {
          end: 1200,
          frontType: 1,
          start: 870
        }
      ],
      surface: 1,
      turn: 1
    },
    "10802": {
      corners: [
        {
          length: 275,
          start: 520
        },
        {
          length: 275,
          start: 795
        }
      ],
      course: 2,
      courseSetStatus: [],
      distance: 1400,
      distanceType: 1,
      finishTimeMax: 84e4,
      finishTimeMin: 8e5,
      laneMax: 14100,
      raceTrackId: 10008,
      slopes: [
        {
          length: 175,
          slope: 15e3,
          start: 320
        },
        {
          length: 150,
          slope: -15e3,
          start: 620
        }
      ],
      straights: [
        {
          end: 520,
          frontType: 2,
          start: 0
        },
        {
          end: 1400,
          frontType: 1,
          start: 1070
        }
      ],
      surface: 1,
      turn: 1
    },
    "10803": {
      corners: [
        {
          length: 250,
          start: 500
        },
        {
          length: 250,
          start: 750
        }
      ],
      course: 3,
      courseSetStatus: [],
      distance: 1400,
      distanceType: 1,
      finishTimeMax: 84e4,
      finishTimeMin: 8e5,
      laneMax: 14100,
      raceTrackId: 10008,
      slopes: [
        {
          length: 100,
          slope: 2e4,
          start: 250
        },
        {
          length: 225,
          slope: 1e4,
          start: 350
        },
        {
          length: 150,
          slope: -2e4,
          start: 575
        }
      ],
      straights: [
        {
          end: 500,
          frontType: 2,
          start: 0
        },
        {
          end: 1400,
          frontType: 1,
          start: 1e3
        }
      ],
      surface: 1,
      turn: 1
    },
    "10804": {
      corners: [
        {
          length: 275,
          start: 720
        },
        {
          length: 275,
          start: 995
        }
      ],
      course: 2,
      courseSetStatus: [
        1
      ],
      distance: 1600,
      distanceType: 2,
      finishTimeMax: 95e4,
      finishTimeMin: 908e3,
      laneMax: 14100,
      raceTrackId: 10008,
      slopes: [
        {
          length: 175,
          slope: 15e3,
          start: 520
        },
        {
          length: 150,
          slope: -15e3,
          start: 820
        }
      ],
      straights: [
        {
          end: 720,
          frontType: 2,
          start: 200
        },
        {
          end: 1600,
          frontType: 1,
          start: 1270
        }
      ],
      surface: 1,
      turn: 1
    },
    "10805": {
      corners: [
        {
          length: 250,
          start: 700
        },
        {
          length: 250,
          start: 950
        }
      ],
      course: 3,
      courseSetStatus: [
        1
      ],
      distance: 1600,
      distanceType: 2,
      finishTimeMax: 95e4,
      finishTimeMin: 908e3,
      laneMax: 14100,
      raceTrackId: 10008,
      slopes: [
        {
          length: 100,
          slope: 2e4,
          start: 450
        },
        {
          length: 225,
          slope: 1e4,
          start: 550
        },
        {
          length: 150,
          slope: -2e4,
          start: 775
        }
      ],
      straights: [
        {
          end: 700,
          frontType: 2,
          start: 200
        },
        {
          end: 1600,
          frontType: 1,
          start: 1200
        }
      ],
      surface: 1,
      turn: 1
    },
    "10806": {
      corners: [
        {
          length: 250,
          start: 900
        },
        {
          length: 250,
          start: 1150
        }
      ],
      course: 3,
      courseSetStatus: [],
      distance: 1800,
      distanceType: 2,
      finishTimeMax: 11e5,
      finishTimeMin: 1044e3,
      laneMax: 14100,
      raceTrackId: 10008,
      slopes: [
        {
          length: 100,
          slope: 2e4,
          start: 650
        },
        {
          length: 225,
          slope: 1e4,
          start: 750
        },
        {
          length: 150,
          slope: -2e4,
          start: 975
        }
      ],
      straights: [
        {
          end: 900,
          frontType: 2,
          start: 400
        },
        {
          end: 1800,
          frontType: 1,
          start: 1400
        }
      ],
      surface: 1,
      turn: 1
    },
    "10807": {
      corners: [
        {
          length: 185,
          start: 400
        },
        {
          length: 185,
          start: 585
        },
        {
          length: 250,
          start: 1170
        },
        {
          length: 250,
          start: 1420
        }
      ],
      course: 2,
      courseSetStatus: [
        3
      ],
      distance: 2e3,
      distanceType: 3,
      finishTimeMax: 123e4,
      finishTimeMin: 1171e3,
      laneMax: 14100,
      raceTrackId: 10008,
      slopes: [
        {
          length: 175,
          slope: 15e3,
          start: 970
        },
        {
          length: 150,
          slope: -15e3,
          start: 1270
        }
      ],
      straights: [
        {
          end: 400,
          frontType: 1,
          start: 0
        },
        {
          end: 1170,
          frontType: 2,
          start: 770
        },
        {
          end: 2e3,
          frontType: 1,
          start: 1670
        }
      ],
      surface: 1,
      turn: 1
    },
    "10808": {
      corners: [
        {
          length: 200,
          start: 400
        },
        {
          length: 200,
          start: 600
        },
        {
          length: 250,
          start: 1300
        },
        {
          length: 250,
          start: 1550
        }
      ],
      course: 3,
      courseSetStatus: [
        1
      ],
      distance: 2200,
      distanceType: 3,
      finishTimeMax: 135e4,
      finishTimeMin: 1302e3,
      laneMax: 14100,
      raceTrackId: 10008,
      slopes: [
        {
          length: 100,
          slope: 2e4,
          start: 1050
        },
        {
          length: 225,
          slope: 1e4,
          start: 1150
        },
        {
          length: 150,
          slope: -2e4,
          start: 1375
        }
      ],
      straights: [
        {
          end: 400,
          frontType: 1,
          start: 0
        },
        {
          end: 1300,
          frontType: 2,
          start: 800
        },
        {
          end: 2200,
          frontType: 1,
          start: 1800
        }
      ],
      surface: 1,
      turn: 1
    },
    "10809": {
      corners: [
        {
          length: 200,
          start: 600
        },
        {
          length: 200,
          start: 800
        },
        {
          length: 250,
          start: 1500
        },
        {
          length: 250,
          start: 1750
        }
      ],
      course: 3,
      courseSetStatus: [
        3
      ],
      distance: 2400,
      distanceType: 3,
      finishTimeMax: 149e4,
      finishTimeMin: 1416e3,
      laneMax: 14100,
      raceTrackId: 10008,
      slopes: [
        {
          length: 100,
          slope: 2e4,
          start: 1250
        },
        {
          length: 225,
          slope: 1e4,
          start: 1350
        },
        {
          length: 150,
          slope: -2e4,
          start: 1575
        }
      ],
      straights: [
        {
          end: 600,
          frontType: 1,
          start: 0
        },
        {
          end: 1500,
          frontType: 2,
          start: 1e3
        },
        {
          end: 2400,
          frontType: 1,
          start: 2e3
        }
      ],
      surface: 1,
      turn: 1
    },
    "10810": {
      corners: [
        {
          length: 250,
          start: 261
        },
        {
          length: 250,
          start: 511
        },
        {
          length: 200,
          start: 1250
        },
        {
          length: 200,
          start: 1450
        },
        {
          length: 250,
          start: 2100
        },
        {
          length: 250,
          start: 2350
        }
      ],
      course: 3,
      courseSetStatus: [
        3,
        5
      ],
      distance: 3e3,
      distanceType: 4,
      finishTimeMax: 19e5,
      finishTimeMin: 1815e3,
      laneMax: 14100,
      raceTrackId: 10008,
      slopes: [
        {
          length: 100,
          slope: 2e4,
          start: 11
        },
        {
          length: 225,
          slope: 1e4,
          start: 111
        },
        {
          length: 150,
          slope: -2e4,
          start: 336
        },
        {
          length: 100,
          slope: 2e4,
          start: 1850
        },
        {
          length: 225,
          slope: 1e4,
          start: 1950
        },
        {
          length: 150,
          slope: -2e4,
          start: 2175
        }
      ],
      straights: [
        {
          end: 261,
          frontType: 2,
          start: 0
        },
        {
          end: 1250,
          frontType: 1,
          start: 761
        },
        {
          end: 2100,
          frontType: 2,
          start: 1650
        },
        {
          end: 3e3,
          frontType: 1,
          start: 2600
        }
      ],
      surface: 1,
      turn: 1
    },
    "10811": {
      corners: [
        {
          length: 250,
          start: 458
        },
        {
          length: 250,
          start: 708
        },
        {
          length: 200,
          start: 1450
        },
        {
          length: 200,
          start: 1650
        },
        {
          length: 250,
          start: 2300
        },
        {
          length: 250,
          start: 2550
        }
      ],
      course: 3,
      courseSetStatus: [],
      distance: 3200,
      distanceType: 4,
      finishTimeMax: 204e4,
      finishTimeMin: 193e4,
      laneMax: 14100,
      raceTrackId: 10008,
      slopes: [
        {
          length: 100,
          slope: 2e4,
          start: 208
        },
        {
          length: 225,
          slope: 1e4,
          start: 308
        },
        {
          length: 150,
          slope: -2e4,
          start: 533
        },
        {
          length: 100,
          slope: 2e4,
          start: 2050
        },
        {
          length: 225,
          slope: 1e4,
          start: 2150
        },
        {
          length: 150,
          slope: -2e4,
          start: 2375
        }
      ],
      straights: [
        {
          end: 458,
          frontType: 2,
          start: 0
        },
        {
          end: 1450,
          frontType: 1,
          start: 958
        },
        {
          end: 2300,
          frontType: 2,
          start: 1850
        },
        {
          end: 3200,
          frontType: 1,
          start: 2800
        }
      ],
      surface: 1,
      turn: 1
    },
    "10812": {
      corners: [
        {
          length: 225,
          start: 400
        },
        {
          length: 225,
          start: 625
        }
      ],
      course: 1,
      courseSetStatus: [],
      distance: 1200,
      distanceType: 1,
      finishTimeMax: 77e4,
      finishTimeMin: 69e4,
      laneMax: 13e3,
      raceTrackId: 10008,
      slopes: [
        {
          length: 200,
          slope: 15e3,
          start: 175
        },
        {
          length: 200,
          slope: -15e3,
          start: 475
        }
      ],
      straights: [
        {
          end: 400,
          frontType: 2,
          start: 0
        },
        {
          end: 1200,
          frontType: 1,
          start: 850
        }
      ],
      surface: 2,
      turn: 1
    },
    "10813": {
      corners: [
        {
          length: 225,
          start: 600
        },
        {
          length: 225,
          start: 825
        }
      ],
      course: 1,
      courseSetStatus: [],
      distance: 1400,
      distanceType: 1,
      finishTimeMax: 94e4,
      finishTimeMin: 808e3,
      laneMax: 13e3,
      raceTrackId: 10008,
      slopes: [
        {
          length: 200,
          slope: 15e3,
          start: 375
        },
        {
          length: 200,
          slope: -15e3,
          start: 675
        }
      ],
      straights: [
        {
          end: 600,
          frontType: 2,
          start: 100
        },
        {
          end: 1400,
          frontType: 1,
          start: 1050
        }
      ],
      surface: 2,
      turn: 1
    },
    "10814": {
      corners: [
        {
          length: 150,
          start: 300
        },
        {
          length: 150,
          start: 450
        },
        {
          length: 225,
          start: 1e3
        },
        {
          length: 225,
          start: 1225
        }
      ],
      course: 1,
      courseSetStatus: [],
      distance: 1800,
      distanceType: 2,
      finishTimeMax: 118e4,
      finishTimeMin: 1081e3,
      laneMax: 13e3,
      raceTrackId: 10008,
      slopes: [
        {
          length: 200,
          slope: 15e3,
          start: 775
        },
        {
          length: 200,
          slope: -15e3,
          start: 1075
        }
      ],
      straights: [
        {
          end: 300,
          frontType: 1,
          start: 0
        },
        {
          end: 1e3,
          frontType: 2,
          start: 600
        },
        {
          end: 1800,
          frontType: 1,
          start: 1450
        }
      ],
      surface: 2,
      turn: 1
    },
    "10815": {
      corners: [
        {
          length: 150,
          start: 400
        },
        {
          length: 150,
          start: 550
        },
        {
          length: 225,
          start: 1100
        },
        {
          length: 225,
          start: 1325
        }
      ],
      course: 1,
      courseSetStatus: [],
      distance: 1900,
      distanceType: 3,
      finishTimeMax: 123e4,
      finishTimeMin: 1142e3,
      laneMax: 13e3,
      raceTrackId: 10008,
      slopes: [
        {
          length: 200,
          slope: 15e3,
          start: 875
        },
        {
          length: 200,
          slope: -15e3,
          start: 1175
        }
      ],
      straights: [
        {
          end: 400,
          frontType: 1,
          start: 0
        },
        {
          end: 1100,
          frontType: 2,
          start: 700
        },
        {
          end: 1900,
          frontType: 1,
          start: 1550
        }
      ],
      surface: 2,
      turn: 1
    },
    "10901": {
      corners: [
        {
          length: 300,
          start: 250
        },
        {
          length: 300,
          start: 550
        }
      ],
      course: 2,
      courseSetStatus: [],
      distance: 1200,
      distanceType: 1,
      finishTimeMax: 71e4,
      finishTimeMin: 675e3,
      laneMax: 12500,
      raceTrackId: 10009,
      slopes: [
        {
          length: 595,
          slope: -1e4,
          start: 400
        },
        {
          length: 125,
          slope: 2e4,
          start: 1e3
        }
      ],
      straights: [
        {
          end: 250,
          frontType: 2,
          start: 0
        },
        {
          end: 1200,
          frontType: 1,
          start: 850
        }
      ],
      surface: 1,
      turn: 1
    },
    "10902": {
      corners: [
        {
          length: 300,
          start: 450
        },
        {
          length: 300,
          start: 750
        }
      ],
      course: 2,
      courseSetStatus: [],
      distance: 1400,
      distanceType: 1,
      finishTimeMax: 84e4,
      finishTimeMin: 8e5,
      laneMax: 12500,
      raceTrackId: 10009,
      slopes: [
        {
          length: 595,
          slope: -1e4,
          start: 600
        },
        {
          length: 125,
          slope: 2e4,
          start: 1200
        }
      ],
      straights: [
        {
          end: 450,
          frontType: 2,
          start: 0
        },
        {
          end: 1400,
          frontType: 1,
          start: 1050
        }
      ],
      surface: 1,
      turn: 1
    },
    "10903": {
      corners: [
        {
          length: 350,
          start: 450
        },
        {
          length: 350,
          start: 800
        }
      ],
      course: 3,
      courseSetStatus: [
        3
      ],
      distance: 1600,
      distanceType: 2,
      finishTimeMax: 95e4,
      finishTimeMin: 908e3,
      laneMax: 12500,
      raceTrackId: 10009,
      slopes: [
        {
          length: 400,
          slope: -1e4,
          start: 950
        },
        {
          length: 120,
          slope: 2e4,
          start: 1405
        }
      ],
      straights: [
        {
          end: 450,
          frontType: 2,
          start: 0
        },
        {
          end: 1600,
          frontType: 1,
          start: 1150
        }
      ],
      surface: 1,
      turn: 1
    },
    "10904": {
      corners: [
        {
          length: 350,
          start: 650
        },
        {
          length: 350,
          start: 1e3
        }
      ],
      course: 3,
      courseSetStatus: [
        3
      ],
      distance: 1800,
      distanceType: 2,
      finishTimeMax: 11e5,
      finishTimeMin: 1044e3,
      laneMax: 12500,
      raceTrackId: 10009,
      slopes: [
        {
          length: 400,
          slope: -1e4,
          start: 1150
        },
        {
          length: 120,
          slope: 2e4,
          start: 1605
        }
      ],
      straights: [
        {
          end: 650,
          frontType: 2,
          start: 0
        },
        {
          end: 1800,
          frontType: 1,
          start: 1350
        }
      ],
      surface: 1,
      turn: 1
    },
    "10905": {
      corners: [
        {
          length: 190,
          start: 370
        },
        {
          length: 190,
          start: 560
        },
        {
          length: 300,
          start: 1050
        },
        {
          length: 300,
          start: 1350
        }
      ],
      course: 2,
      courseSetStatus: [
        4
      ],
      distance: 2e3,
      distanceType: 3,
      finishTimeMax: 123e4,
      finishTimeMin: 1171e3,
      laneMax: 12500,
      raceTrackId: 10009,
      slopes: [
        {
          length: 140,
          slope: -1e4,
          start: 0
        },
        {
          length: 125,
          slope: 2e4,
          start: 145
        },
        {
          length: 595,
          slope: -1e4,
          start: 1200
        },
        {
          length: 125,
          slope: 2e4,
          start: 1800
        }
      ],
      straights: [
        {
          end: 370,
          frontType: 1,
          start: 0
        },
        {
          end: 1050,
          frontType: 2,
          start: 750
        },
        {
          end: 2e3,
          frontType: 1,
          start: 1650
        }
      ],
      surface: 1,
      turn: 1
    },
    "10906": {
      corners: [
        {
          length: 190,
          start: 520
        },
        {
          length: 190,
          start: 710
        },
        {
          length: 300,
          start: 1250
        },
        {
          length: 300,
          start: 1550
        }
      ],
      course: 2,
      courseSetStatus: [
        1
      ],
      distance: 2200,
      distanceType: 3,
      finishTimeMax: 135e4,
      finishTimeMin: 1302e3,
      laneMax: 12500,
      raceTrackId: 10009,
      slopes: [
        {
          length: 290,
          slope: -1e4,
          start: 0
        },
        {
          length: 125,
          slope: 2e4,
          start: 295
        },
        {
          length: 595,
          slope: -1e4,
          start: 1400
        },
        {
          length: 125,
          slope: 2e4,
          start: 2e3
        }
      ],
      straights: [
        {
          end: 520,
          frontType: 1,
          start: 0
        },
        {
          end: 1250,
          frontType: 2,
          start: 900
        },
        {
          end: 2200,
          frontType: 1,
          start: 1850
        }
      ],
      surface: 1,
      turn: 1
    },
    "10907": {
      corners: [
        {
          length: 190,
          start: 382
        },
        {
          length: 190,
          start: 558
        },
        {
          length: 350,
          start: 1250
        },
        {
          length: 350,
          start: 1600
        }
      ],
      course: 3,
      courseSetStatus: [
        3
      ],
      distance: 2400,
      distanceType: 3,
      finishTimeMax: 149e4,
      finishTimeMin: 1416e3,
      laneMax: 12500,
      raceTrackId: 10009,
      slopes: [
        {
          length: 132,
          slope: -1e4,
          start: 0
        },
        {
          length: 120,
          slope: 2e4,
          start: 187
        },
        {
          length: 400,
          slope: -1e4,
          start: 1750
        },
        {
          length: 120,
          slope: 2e4,
          start: 2205
        }
      ],
      straights: [
        {
          end: 1250,
          frontType: 2,
          start: 750
        },
        {
          end: 2400,
          frontType: 1,
          start: 1950
        }
      ],
      surface: 1,
      turn: 1
    },
    "10908": {
      corners: [
        {
          length: 190,
          start: 570
        },
        {
          length: 190,
          start: 760
        },
        {
          length: 350,
          start: 1450
        },
        {
          length: 350,
          start: 1800
        }
      ],
      course: 3,
      courseSetStatus: [],
      distance: 2600,
      distanceType: 4,
      finishTimeMax: 165e4,
      finishTimeMin: 1576e3,
      laneMax: 12500,
      raceTrackId: 10009,
      slopes: [
        {
          length: 315,
          slope: -1e4,
          start: 0
        },
        {
          length: 120,
          slope: 2e4,
          start: 370
        },
        {
          length: 400,
          slope: -1e4,
          start: 1950
        },
        {
          length: 120,
          slope: 2e4,
          start: 2405
        }
      ],
      straights: [
        {
          end: 570,
          frontType: 1,
          start: 0
        },
        {
          end: 1450,
          frontType: 2,
          start: 950
        },
        {
          end: 2600,
          frontType: 1,
          start: 2150
        }
      ],
      surface: 1,
      turn: 1
    },
    "10909": {
      corners: [
        {
          length: 300,
          start: 348
        },
        {
          length: 300,
          start: 648
        },
        {
          length: 190,
          start: 1320
        },
        {
          length: 190,
          start: 1510
        },
        {
          length: 300,
          start: 2050
        },
        {
          length: 300,
          start: 2350
        }
      ],
      course: 2,
      courseSetStatus: [
        3
      ],
      distance: 3e3,
      distanceType: 4,
      finishTimeMax: 19e5,
      finishTimeMin: 1815e3,
      laneMax: 12500,
      raceTrackId: 10009,
      slopes: [
        {
          length: 595,
          slope: -1e4,
          start: 498
        },
        {
          length: 125,
          slope: 2e4,
          start: 1095
        },
        {
          length: 595,
          slope: -1e4,
          start: 2200
        },
        {
          length: 125,
          slope: 2e4,
          start: 2800
        }
      ],
      straights: [
        {
          end: 348,
          frontType: 2,
          start: 0
        },
        {
          end: 1320,
          frontType: 1,
          start: 948
        },
        {
          end: 2050,
          frontType: 2,
          start: 1700
        },
        {
          end: 3e3,
          frontType: 1,
          start: 2650
        }
      ],
      surface: 1,
      turn: 1
    },
    "10910": {
      corners: [
        {
          length: 250,
          start: 350
        },
        {
          length: 250,
          start: 600
        }
      ],
      course: 1,
      courseSetStatus: [],
      distance: 1200,
      distanceType: 1,
      finishTimeMax: 77e4,
      finishTimeMin: 69e4,
      laneMax: 12e3,
      raceTrackId: 10009,
      slopes: [],
      straights: [
        {
          end: 350,
          frontType: 2,
          start: 0
        },
        {
          end: 1200,
          frontType: 1,
          start: 850
        }
      ],
      surface: 2,
      turn: 1
    },
    "10911": {
      corners: [
        {
          length: 250,
          start: 550
        },
        {
          length: 250,
          start: 800
        }
      ],
      course: 1,
      courseSetStatus: [],
      distance: 1400,
      distanceType: 1,
      finishTimeMax: 94e4,
      finishTimeMin: 808e3,
      laneMax: 12e3,
      raceTrackId: 10009,
      slopes: [
        {
          length: 125,
          slope: 15e3,
          start: 1200
        }
      ],
      straights: [
        {
          end: 550,
          frontType: 2,
          start: 0
        },
        {
          end: 1400,
          frontType: 1,
          start: 1050
        }
      ],
      surface: 2,
      turn: 1
    },
    "10912": {
      corners: [
        {
          length: 150,
          start: 330
        },
        {
          length: 150,
          start: 480
        },
        {
          length: 250,
          start: 950
        },
        {
          length: 250,
          start: 1200
        }
      ],
      course: 1,
      courseSetStatus: [],
      distance: 1800,
      distanceType: 2,
      finishTimeMax: 118e4,
      finishTimeMin: 1081e3,
      laneMax: 12e3,
      raceTrackId: 10009,
      slopes: [
        {
          length: 125,
          slope: 15e3,
          start: 105
        },
        {
          length: 125,
          slope: 15e3,
          start: 1599
        }
      ],
      straights: [
        {
          end: 330,
          frontType: 1,
          start: 0
        },
        {
          end: 950,
          frontType: 2,
          start: 630
        },
        {
          end: 1800,
          frontType: 1,
          start: 1449
        }
      ],
      surface: 2,
      turn: 1
    },
    "10913": {
      corners: [
        {
          length: 150,
          start: 500
        },
        {
          length: 150,
          start: 650
        },
        {
          length: 250,
          start: 1150
        },
        {
          length: 250,
          start: 1400
        }
      ],
      course: 1,
      courseSetStatus: [
        2,
        3
      ],
      distance: 2e3,
      distanceType: 3,
      finishTimeMax: 129e4,
      finishTimeMin: 1219e3,
      laneMax: 12e3,
      raceTrackId: 10009,
      slopes: [
        {
          length: 125,
          slope: 15e3,
          start: 275
        },
        {
          length: 125,
          slope: 15e3,
          start: 1800
        }
      ],
      straights: [
        {
          end: 500,
          frontType: 1,
          start: 0
        },
        {
          end: 1150,
          frontType: 2,
          start: 800
        },
        {
          end: 2e3,
          frontType: 1,
          start: 1650
        }
      ],
      surface: 2,
      turn: 1
    },
    "10914": {
      corners: [
        {
          length: 350,
          start: 370
        },
        {
          length: 350,
          start: 720
        },
        {
          length: 190,
          start: 1520
        },
        {
          length: 190,
          start: 1710
        },
        {
          length: 300,
          start: 2250
        },
        {
          length: 300,
          start: 2550
        }
      ],
      course: 4,
      courseSetStatus: [],
      distance: 3200,
      distanceType: 4,
      finishTimeMax: 204e4,
      finishTimeMin: 193e4,
      laneMax: 12500,
      raceTrackId: 10009,
      slopes: [
        {
          length: 400,
          slope: -1e4,
          start: 870
        },
        {
          length: 120,
          slope: 2e4,
          start: 1325
        },
        {
          length: 595,
          slope: -1e4,
          start: 2400
        },
        {
          length: 125,
          slope: 2e4,
          start: 3e3
        }
      ],
      straights: [
        {
          end: 370,
          frontType: 2,
          start: 0
        },
        {
          end: 1520,
          frontType: 1,
          start: 1070
        },
        {
          end: 2250,
          frontType: 2,
          start: 1900
        },
        {
          end: 3200,
          frontType: 1,
          start: 2850
        }
      ],
      surface: 1,
      turn: 1
    },
    "11001": {
      corners: [
        {
          length: 205,
          start: 500
        },
        {
          length: 205,
          start: 705
        }
      ],
      course: 1,
      courseSetStatus: [
        1
      ],
      distance: 1200,
      distanceType: 1,
      finishTimeMax: 71e4,
      finishTimeMin: 675e3,
      laneMax: 14500,
      raceTrackId: 10010,
      slopes: [
        {
          length: 60,
          slope: -15e3,
          start: 0
        }
      ],
      straights: [
        {
          end: 500,
          frontType: 2,
          start: 0
        },
        {
          end: 1200,
          frontType: 1,
          start: 910
        }
      ],
      surface: 1,
      turn: 1
    },
    "11002": {
      corners: [
        {
          length: 205,
          start: 290
        },
        {
          length: 205,
          start: 495
        },
        {
          length: 205,
          start: 1100
        },
        {
          length: 205,
          start: 1305
        }
      ],
      course: 1,
      courseSetStatus: [],
      distance: 1800,
      distanceType: 2,
      finishTimeMax: 11e5,
      finishTimeMin: 1044e3,
      laneMax: 14500,
      raceTrackId: 10010,
      slopes: [
        {
          length: 255,
          slope: 15e3,
          start: 280
        }
      ],
      straights: [
        {
          end: 290,
          frontType: 1,
          start: 0
        },
        {
          end: 1100,
          frontType: 2,
          start: 700
        },
        {
          end: 1800,
          frontType: 1,
          start: 1510
        }
      ],
      surface: 1,
      turn: 1
    },
    "11003": {
      corners: [
        {
          length: 205,
          start: 490
        },
        {
          length: 205,
          start: 695
        },
        {
          length: 205,
          start: 1300
        },
        {
          length: 205,
          start: 1505
        }
      ],
      course: 1,
      courseSetStatus: [
        3
      ],
      distance: 2e3,
      distanceType: 3,
      finishTimeMax: 123e4,
      finishTimeMin: 1171e3,
      laneMax: 14500,
      raceTrackId: 10010,
      slopes: [
        {
          length: 255,
          slope: 15e3,
          start: 480
        }
      ],
      straights: [
        {
          end: 490,
          frontType: 1,
          start: 0
        },
        {
          end: 1300,
          frontType: 2,
          start: 900
        },
        {
          end: 2e3,
          frontType: 1,
          start: 1710
        }
      ],
      surface: 1,
      turn: 1
    },
    "11004": {
      corners: [
        {
          length: 205,
          start: 309
        },
        {
          length: 205,
          start: 514
        },
        {
          length: 205,
          start: 1110
        },
        {
          length: 205,
          start: 1315
        },
        {
          length: 205,
          start: 1900
        },
        {
          length: 205,
          start: 2105
        }
      ],
      course: 1,
      courseSetStatus: [
        2
      ],
      distance: 2600,
      distanceType: 4,
      finishTimeMax: 165e4,
      finishTimeMin: 1576e3,
      laneMax: 14500,
      raceTrackId: 10010,
      slopes: [
        {
          length: 255,
          slope: 15e3,
          start: 1100
        }
      ],
      straights: [
        {
          end: 309,
          frontType: 2,
          start: 0
        },
        {
          end: 1110,
          frontType: 1,
          start: 719
        },
        {
          end: 1900,
          frontType: 2,
          start: 1520
        },
        {
          end: 2600,
          frontType: 1,
          start: 2310
        }
      ],
      surface: 1,
      turn: 1
    },
    "11005": {
      corners: [
        {
          length: 180,
          start: 360
        },
        {
          length: 180,
          start: 540
        }
      ],
      course: 1,
      courseSetStatus: [
        1
      ],
      distance: 1e3,
      distanceType: 1,
      finishTimeMax: 63e4,
      finishTimeMin: 574e3,
      laneMax: 12e3,
      raceTrackId: 10010,
      slopes: [],
      straights: [
        {
          end: 360,
          frontType: 2,
          start: 0
        },
        {
          end: 1e3,
          frontType: 1,
          start: 720
        }
      ],
      surface: 2,
      turn: 1
    },
    "11006": {
      corners: [
        {
          length: 180,
          start: 340
        },
        {
          length: 180,
          start: 520
        },
        {
          length: 180,
          start: 1060
        },
        {
          length: 180,
          start: 1240
        }
      ],
      course: 1,
      courseSetStatus: [],
      distance: 1700,
      distanceType: 2,
      finishTimeMax: 113e4,
      finishTimeMin: 1014e3,
      laneMax: 12e3,
      raceTrackId: 10010,
      slopes: [
        {
          length: 150,
          slope: 15e3,
          start: 370
        }
      ],
      straights: [
        {
          end: 340,
          frontType: 1,
          start: 0
        },
        {
          end: 1060,
          frontType: 2,
          start: 700
        },
        {
          end: 1700,
          frontType: 1,
          start: 1420
        }
      ],
      surface: 2,
      turn: 1
    },
    "11007": {
      corners: [
        {
          length: 180,
          start: 312
        },
        {
          length: 180,
          start: 492
        },
        {
          length: 180,
          start: 1040
        },
        {
          length: 180,
          start: 1220
        },
        {
          length: 180,
          start: 1760
        },
        {
          length: 180,
          start: 1940
        }
      ],
      course: 1,
      courseSetStatus: [],
      distance: 2400,
      distanceType: 3,
      finishTimeMax: 156e4,
      finishTimeMin: 1491e3,
      laneMax: 12e3,
      raceTrackId: 10010,
      slopes: [],
      straights: [
        {
          end: 312,
          frontType: 2,
          start: 0
        },
        {
          end: 1040,
          frontType: 1,
          start: 672
        },
        {
          end: 1760,
          frontType: 2,
          start: 1400
        },
        {
          end: 2400,
          frontType: 1,
          start: 2120
        }
      ],
      surface: 2,
      turn: 1
    },
    "11101": {
      corners: [
        {
          length: 150,
          start: 500
        },
        {
          length: 150,
          start: 650
        }
      ],
      course: 1,
      courseSetStatus: [],
      distance: 1200,
      distanceType: 1,
      finishTimeMax: 77e4,
      finishTimeMin: 69e4,
      laneMax: 12e3,
      raceTrackId: 10101,
      slopes: [],
      straights: [
        {
          end: 500,
          frontType: 2,
          start: 0
        },
        {
          end: 1200,
          frontType: 1,
          start: 812
        }
      ],
      surface: 2,
      turn: 1
    },
    "11102": {
      corners: [
        {
          length: 150,
          start: 300
        },
        {
          length: 150,
          start: 500
        },
        {
          length: 150,
          start: 1100
        },
        {
          length: 150,
          start: 1250
        }
      ],
      course: 1,
      courseSetStatus: [],
      distance: 1800,
      distanceType: 2,
      finishTimeMax: 118e4,
      finishTimeMin: 1081e3,
      laneMax: 12e3,
      raceTrackId: 10101,
      slopes: [],
      straights: [
        {
          end: 301,
          frontType: 1,
          start: 0
        },
        {
          end: 1100.22998046875,
          frontType: 2,
          start: 600
        },
        {
          end: 1800,
          frontType: 1,
          start: 1400
        }
      ],
      surface: 2,
      turn: 1
    },
    "11103": {
      corners: [
        {
          length: 150,
          start: 500
        },
        {
          length: 150,
          start: 650
        },
        {
          length: 150,
          start: 1300
        },
        {
          length: 150,
          start: 1450
        }
      ],
      course: 1,
      courseSetStatus: [],
      distance: 2e3,
      distanceType: 3,
      finishTimeMax: 129e4,
      finishTimeMin: 1219e3,
      laneMax: 12e3,
      raceTrackId: 10101,
      slopes: [],
      straights: [
        {
          end: 500,
          frontType: 1,
          start: 0
        },
        {
          end: 1300,
          frontType: 2,
          start: 800
        },
        {
          end: 2e3,
          frontType: 1,
          start: 1600
        }
      ],
      surface: 2,
      turn: 1
    }
  };

  // vendor/kachi-uma-tools/uma-skill-tools/CourseData.ts
  var Surface = /* @__PURE__ */ ((Surface2) => {
    Surface2[Surface2["Turf"] = 1] = "Turf";
    Surface2[Surface2["Dirt"] = 2] = "Dirt";
    return Surface2;
  })(Surface || {});
  var DistanceType = /* @__PURE__ */ ((DistanceType3) => {
    DistanceType3[DistanceType3["Short"] = 1] = "Short";
    DistanceType3[DistanceType3["Mile"] = 2] = "Mile";
    DistanceType3[DistanceType3["Mid"] = 3] = "Mid";
    DistanceType3[DistanceType3["Long"] = 4] = "Long";
    return DistanceType3;
  })(DistanceType || {});
  var Orientation = /* @__PURE__ */ ((Orientation2) => {
    Orientation2[Orientation2["Clockwise"] = 1] = "Clockwise";
    Orientation2[Orientation2["Counterclockwise"] = 2] = "Counterclockwise";
    Orientation2[Orientation2["UnusedOrientation"] = 3] = "UnusedOrientation";
    Orientation2[Orientation2["NoTurns"] = 4] = "NoTurns";
    return Orientation2;
  })(Orientation || {});
  var CourseHelpers;
  ((CourseHelpers2) => {
    function assertIsPhase(phase) {
      strict(phase == 0 || phase == 1 || phase == 2 || phase == 3);
    }
    CourseHelpers2.assertIsPhase = assertIsPhase;
    function assertIsSurface(surface) {
      strict(Surface.hasOwnProperty(surface));
    }
    CourseHelpers2.assertIsSurface = assertIsSurface;
    function assertIsDistanceType(distanceType) {
      strict(DistanceType.hasOwnProperty(distanceType));
    }
    CourseHelpers2.assertIsDistanceType = assertIsDistanceType;
    function assertIsOrientation(orientation) {
      strict(Orientation.hasOwnProperty(orientation));
    }
    CourseHelpers2.assertIsOrientation = assertIsOrientation;
    function isSortedByStart(arr) {
      const init = [true, -1];
      function isSorted(a, b) {
        return [a[0] && b.start > a[1], b.start];
      }
      return arr.reduce(isSorted, init)[0];
    }
    CourseHelpers2.isSortedByStart = isSortedByStart;
    function phaseStart(distance, phase) {
      switch (phase) {
        case 0:
          return 0;
        case 1:
          return distance * 1 / 6;
        case 2:
          return distance * 2 / 3;
        case 3:
          return distance * 5 / 6;
      }
    }
    CourseHelpers2.phaseStart = phaseStart;
    function phaseEnd(distance, phase) {
      switch (phase) {
        case 0:
          return distance * 1 / 6;
        case 1:
          return distance * 2 / 3;
        case 2:
          return distance * 5 / 6;
        case 3:
          return distance;
      }
    }
    CourseHelpers2.phaseEnd = phaseEnd;
    function courseSpeedModifier(course, stats) {
      const statvalues = [0, stats.speed, stats.stamina, stats.power, stats.guts, stats.wisdom].map((x) => Math.min(x, 901));
      return 1 + course.courseSetStatus.map(
        (stat) => (1 + Math.floor(statvalues[stat] / 300.01)) * 0.05
      ).reduce((a, b) => a + b, 0) / Math.max(course.courseSetStatus.length, 1);
    }
    CourseHelpers2.courseSpeedModifier = courseSpeedModifier;
    function getCourse(courseId) {
      const course = course_data_default[courseId];
      if (!isSortedByStart(course.slopes))
        course.slopes.sort((a, b) => a.start - b.start);
      const courseWidth = 11.25;
      const horseLane = courseWidth / 18;
      const laneChangeAcceleration = 0.02 * 1.5;
      const laneChangeAccelerationPerFrame = laneChangeAcceleration / 15;
      const maxLaneDistance = courseWidth * course.laneMax / 1e4;
      const moveLanePoint = course.corners.length > 0 ? course.corners[0].start : 30;
      const course2 = {
        ...course,
        courseWidth,
        horseLane,
        laneChangeAcceleration,
        laneChangeAccelerationPerFrame,
        maxLaneDistance,
        moveLanePoint
      };
      Object.keys(course2).forEach((k) => Object.freeze(course2[k]));
      return Object.freeze(course2);
    }
    CourseHelpers2.getCourse = getCourse;
  })(CourseHelpers || (CourseHelpers = {}));

  // vendor/kachi-uma-tools/uma-skill-tools/RaceSolverBuilder.ts
  init_Region();

  // node_modules/prando/dist/Prando.es.js
  var Prando = (
    /** @class */
    function() {
      function Prando2(seed) {
        this._value = NaN;
        if (typeof seed === "string") {
          this._seed = this.hashCode(seed);
        } else if (typeof seed === "number") {
          this._seed = this.getSafeSeed(seed);
        } else {
          this._seed = this.getSafeSeed(Prando2.MIN + Math.floor((Prando2.MAX - Prando2.MIN) * Math.random()));
        }
        this.reset();
      }
      Prando2.prototype.next = function(min, pseudoMax) {
        if (min === void 0) {
          min = 0;
        }
        if (pseudoMax === void 0) {
          pseudoMax = 1;
        }
        this.recalculate();
        return this.map(this._value, Prando2.MIN, Prando2.MAX, min, pseudoMax);
      };
      Prando2.prototype.nextInt = function(min, max) {
        if (min === void 0) {
          min = 10;
        }
        if (max === void 0) {
          max = 100;
        }
        this.recalculate();
        return Math.floor(this.map(this._value, Prando2.MIN, Prando2.MAX, min, max + 1));
      };
      Prando2.prototype.nextString = function(length, chars) {
        if (length === void 0) {
          length = 16;
        }
        if (chars === void 0) {
          chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
        }
        var str = "";
        while (str.length < length) {
          str += this.nextChar(chars);
        }
        return str;
      };
      Prando2.prototype.nextChar = function(chars) {
        if (chars === void 0) {
          chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
        }
        return chars.substr(this.nextInt(0, chars.length - 1), 1);
      };
      Prando2.prototype.nextArrayItem = function(array) {
        return array[this.nextInt(0, array.length - 1)];
      };
      Prando2.prototype.nextBoolean = function() {
        this.recalculate();
        return this._value > 0.5;
      };
      Prando2.prototype.skip = function(iterations) {
        if (iterations === void 0) {
          iterations = 1;
        }
        while (iterations-- > 0) {
          this.recalculate();
        }
      };
      Prando2.prototype.reset = function() {
        this._value = this._seed;
      };
      Prando2.prototype.recalculate = function() {
        this._value = this.xorshift(this._value);
      };
      Prando2.prototype.xorshift = function(value) {
        value ^= value << 13;
        value ^= value >> 17;
        value ^= value << 5;
        return value;
      };
      Prando2.prototype.map = function(val, minFrom, maxFrom, minTo, maxTo) {
        return (val - minFrom) / (maxFrom - minFrom) * (maxTo - minTo) + minTo;
      };
      Prando2.prototype.hashCode = function(str) {
        var hash = 0;
        if (str) {
          var l = str.length;
          for (var i = 0; i < l; i++) {
            hash = (hash << 5) - hash + str.charCodeAt(i);
            hash |= 0;
            hash = this.xorshift(hash);
          }
        }
        return this.getSafeSeed(hash);
      };
      Prando2.prototype.getSafeSeed = function(seed) {
        if (seed === 0)
          return 1;
        return seed;
      };
      Prando2.MIN = -2147483648;
      Prando2.MAX = 2147483647;
      return Prando2;
    }()
  );
  var Prando_es_default = Prando;

  // vendor/kachi-uma-tools/uma-skill-tools/Random.ts
  var SeededRng = class {
    prando;
    constructor(seed) {
      this.prando = new Prando_es_default(seed);
    }
    int32() {
      return Math.floor(this.prando.next() * 4294967296);
    }
    random() {
      return this.prando.next();
    }
    uniform(upper) {
      return this.prando.nextInt(0, upper - 1);
    }
  };
  var Rule30CARng = SeededRng;

  // vendor/kachi-uma-tools/uma-skill-tools/ActivationConditions.ts
  init_Region();
  init_ActivationSamplePolicy();
  function kTrue(_) {
    return true;
  }
  function withDefaultCond(r) {
    if (r instanceof RegionList) {
      return [r, kTrue];
    }
    return r;
  }
  var EqOperator = class {
    constructor(condition, argument) {
      this.condition = condition;
      this.argument = argument;
      this.samplePolicy = condition.samplePolicy;
    }
    samplePolicy;
    apply(regions, course, horse, extra) {
      return withDefaultCond(this.condition.filterEq(regions, this.argument, course, horse, extra));
    }
  };
  var NeqOperator = class {
    constructor(condition, argument) {
      this.condition = condition;
      this.argument = argument;
      this.samplePolicy = condition.samplePolicy;
    }
    samplePolicy;
    apply(regions, course, horse, extra) {
      return withDefaultCond(this.condition.filterNeq(regions, this.argument, course, horse, extra));
    }
  };
  var LtOperator = class {
    constructor(condition, argument) {
      this.condition = condition;
      this.argument = argument;
      this.samplePolicy = condition.samplePolicy;
    }
    samplePolicy;
    apply(regions, course, horse, extra) {
      return withDefaultCond(this.condition.filterLt(regions, this.argument, course, horse, extra));
    }
  };
  var LteOperator = class {
    constructor(condition, argument) {
      this.condition = condition;
      this.argument = argument;
      this.samplePolicy = condition.samplePolicy;
    }
    samplePolicy;
    apply(regions, course, horse, extra) {
      return withDefaultCond(this.condition.filterLte(regions, this.argument, course, horse, extra));
    }
  };
  var GtOperator = class {
    constructor(condition, argument) {
      this.condition = condition;
      this.argument = argument;
      this.samplePolicy = condition.samplePolicy;
    }
    samplePolicy;
    apply(regions, course, horse, extra) {
      return withDefaultCond(this.condition.filterGt(regions, this.argument, course, horse, extra));
    }
  };
  var GteOperator = class {
    constructor(condition, argument) {
      this.condition = condition;
      this.argument = argument;
      this.samplePolicy = condition.samplePolicy;
    }
    samplePolicy;
    apply(regions, course, horse, extra) {
      return withDefaultCond(this.condition.filterGte(regions, this.argument, course, horse, extra));
    }
  };
  var AndOperator = class {
    constructor(left, right) {
      this.left = left;
      this.right = right;
      this.samplePolicy = left.samplePolicy.reconcile(right.samplePolicy);
    }
    samplePolicy;
    apply(regions, course, horse, extra) {
      const [leftval, leftcond] = this.left.apply(regions, course, horse, extra);
      const [rightval, rightcond] = this.right.apply(leftval, course, horse, extra);
      if (leftcond === kTrue && rightcond === kTrue) {
        return [rightval, kTrue];
      }
      return [rightval, (s) => leftcond(s) && rightcond(s)];
    }
  };
  var OrOperator = class {
    constructor(left, right) {
      this.left = left;
      this.right = right;
      this.samplePolicy = left.samplePolicy.reconcile(right.samplePolicy);
    }
    samplePolicy;
    apply(regions, course, horse, extra) {
      const [leftval, leftcond] = this.left.apply(regions, course, horse, extra);
      const [rightval, rightcond] = this.right.apply(regions, course, horse, extra);
      return [leftval.union(rightval), (s) => leftcond(s) || rightcond(s)];
    }
  };
  function notSupported(_0, _1, _2, _3, extra) {
    strict(false, "unsupported comparison");
    throw 0;
  }
  function noop(regions, _1, _2, _3, extra) {
    return regions;
  }
  var noopAll = Object.freeze({
    filterEq: noop,
    filterNeq: noop,
    filterLt: noop,
    filterLte: noop,
    filterGt: noop,
    filterGte: noop
  });
  var noopImmediate = Object.freeze(Object.assign({ samplePolicy: ImmediatePolicy }, noopAll));
  var noopRandom = Object.freeze(Object.assign({ samplePolicy: RandomPolicy }, noopAll));
  var defaultImmediate = Object.freeze({
    samplePolicy: ImmediatePolicy,
    filterEq: notSupported,
    filterNeq: notSupported,
    filterLt: notSupported,
    filterLte: notSupported,
    filterGt: notSupported,
    filterGte: notSupported
  });
  function immediate(o) {
    return Object.assign({}, defaultImmediate, o);
  }
  var defaultRandom = Object.freeze({
    samplePolicy: RandomPolicy,
    filterEq: notSupported,
    filterNeq: notSupported,
    filterLt: notSupported,
    filterLte: notSupported,
    filterGt: notSupported,
    filterGte: notSupported
  });
  function random(o) {
    return Object.assign({}, defaultRandom, o);
  }
  function distributionRandomFactory(cls) {
    const cache = /* @__PURE__ */ Object.create(null);
    return function(...args) {
      const o = args.pop();
      const key = args.join(",");
      const policy = key in cache ? cache[key] : cache[key] = Object.freeze(new cls(...args));
      return Object.assign({
        samplePolicy: policy,
        filterEq: notSupported,
        filterNeq: notSupported,
        filterLt: notSupported,
        filterLte: notSupported,
        filterGt: notSupported,
        filterGte: notSupported
      }, o);
    };
  }
  var logNormalRandom = distributionRandomFactory(LogNormalRandomPolicy);
  var erlangRandom = distributionRandomFactory(ErlangRandomPolicy);
  var uniformRandom = distributionRandomFactory(UniformRandomPolicy);
  function noopErlangRandom(k, lambda) {
    return erlangRandom(k, lambda, noopAll);
  }
  var noopUniformRandom = uniformRandom(noopAll);
  function shiftRegionsForwardByMinTime(regions, minTime, course, _, extra) {
    const minDistance = 13 * minTime;
    const shiftedRegions = new RegionList();
    regions.forEach((r) => {
      if (r.start === 0) {
        shiftedRegions.push(new Region(r.start + minDistance, r.end));
      } else {
        shiftedRegions.push(r);
      }
    });
    return shiftedRegions.length > 0 ? shiftedRegions : new RegionList();
  }
  function noopSectionRandom(start, end) {
    function sectionRandom(regions, _0, course, _1, extra) {
      const bounds = new Region(start * (course.distance / 24), end * (course.distance / 24));
      return regions.rmap((r) => r.intersect(bounds));
    }
    return random({
      filterEq: sectionRandom,
      filterNeq: sectionRandom,
      filterLt: sectionRandom,
      filterLte: sectionRandom,
      filterGt: sectionRandom,
      filterGte: sectionRandom
    });
  }
  function valueFilter(getValue) {
    return immediate({
      filterEq(regions, value, course, horse, extra) {
        return getValue(course, horse, extra) == value ? regions : new RegionList();
      },
      filterNeq(regions, value, course, horse, extra) {
        return getValue(course, horse, extra) != value ? regions : new RegionList();
      },
      filterLt(regions, value, course, horse, extra) {
        return getValue(course, horse, extra) < value ? regions : new RegionList();
      },
      filterLte(regions, value, course, horse, extra) {
        return getValue(course, horse, extra) <= value ? regions : new RegionList();
      },
      filterGt(regions, value, course, horse, extra) {
        return getValue(course, horse, extra) > value ? regions : new RegionList();
      },
      filterGte(regions, value, course, horse, extra) {
        return getValue(course, horse, extra) >= value ? regions : new RegionList();
      }
    });
  }
  function orderFilter(getPos) {
    return immediate({
      filterEq(regions, arg, _0, _1, extra) {
        if (extra.orderRange != null) {
          const pos = getPos(arg, extra.numUmas);
          return pos >= extra.orderRange[0] && pos <= extra.orderRange[1] ? regions : new RegionList();
        }
        return regions;
      },
      filterNeq(regions, arg, _0, _1, extra) {
        if (extra.orderRange != null) {
          const pos = getPos(arg, extra.numUmas);
          return pos < extra.orderRange[0] || pos > extra.orderRange[1] ? regions : new RegionList();
        }
        return regions;
      },
      filterLt(regions, arg, course, _, extra) {
        if (extra.orderRange != null) {
          strict(1 <= extra.orderRange[0] && extra.orderRange[0] <= extra.orderRange[1]);
          const end = new Region(CourseHelpers.phaseStart(course.distance, 2) + 100, course.distance);
          const pos = getPos(arg, extra.numUmas);
          return extra.orderRange[0] < pos ? regions : regions.rmap((r) => r.intersect(end));
        }
        return regions;
      },
      filterLte(regions, arg, course, _, extra) {
        if (extra.orderRange != null) {
          strict(1 <= extra.orderRange[0] && extra.orderRange[0] <= extra.orderRange[1]);
          const end = new Region(CourseHelpers.phaseStart(course.distance, 2) + 100, course.distance);
          const pos = getPos(arg, extra.numUmas);
          return extra.orderRange[0] <= pos ? regions : regions.rmap((r) => r.intersect(end));
        }
        return regions;
      },
      filterGt(regions, arg, _0, _1, extra) {
        if (extra.orderRange != null) {
          strict(extra.orderRange[0] <= extra.orderRange[1] && extra.orderRange[1] <= extra.numUmas);
          const pos = getPos(arg, extra.numUmas);
          return pos < extra.orderRange[1] ? regions : new RegionList();
        }
        return regions;
      },
      filterGte(regions, arg, _0, _1, extra) {
        if (extra.orderRange != null) {
          strict(extra.orderRange[0] <= extra.orderRange[1] && extra.orderRange[1] <= extra.numUmas);
          const pos = getPos(arg, extra.numUmas);
          return pos <= extra.orderRange[1] ? regions : new RegionList();
        }
        return regions;
      }
    });
  }
  function orderInFilter(rate) {
    return immediate({
      filterEq(regions, one, _0, _1, extra) {
        strict(one == 1, "must be order_rate_inXX_continue==1");
        if (extra.orderRange != null) {
          strict(1 <= extra.orderRange[0] && extra.orderRange[0] <= extra.orderRange[1]);
          return extra.orderRange[0] <= Math.round(rate * extra.numUmas) ? regions : new RegionList();
        }
        return regions;
      }
    });
  }
  function orderOutFilter(rate) {
    return immediate({
      filterEq(regions, one, _0, _1, extra) {
        strict(one == 1, "must be order_rate_outXX_continue==1");
        if (extra.orderRange != null) {
          strict(extra.orderRange[0] <= extra.orderRange[1] && extra.orderRange[1] <= extra.numUmas);
          return Math.round(rate * extra.numUmas) <= extra.orderRange[1] ? regions : new RegionList();
        }
        return regions;
      }
    });
  }
  var Conditions = Object.freeze({
    accumulatetime: immediate({
      filterGte(regions, t, course, _, extra) {
        const baseSpeed2 = 20 - (course.distance - 2e3) / 1e3;
        const rest = new Region(0.85 * baseSpeed2 * t, course.distance);
        return [regions.rmap((r) => r.intersect(rest)), (s) => s.accumulatetime.t >= t];
      }
    }),
    activate_count_all: immediate({
      filterLte(regions, n, _0, _1, extra) {
        return [regions, (s) => s.activateCount.reduce((a, b) => a + b) <= n];
      },
      filterGte(regions, n, _0, _1, extra) {
        return [regions, (s) => s.activateCount.reduce((a, b) => a + b) >= n];
      }
    }),
    activate_count_end_after: immediate({
      filterGte(regions, n, _0, _1, extra) {
        return [regions, (s) => s.activateCount[2] >= n];
      }
    }),
    activate_count_heal: immediate({
      filterGte(regions, n, _0, _1, extra) {
        return [regions, (s) => s.activateCountHeal >= n];
      }
    }),
    activate_count_middle: immediate({
      filterGte(regions, n, _0, _1, extra) {
        return [regions, (s) => s.activateCount[1] >= n];
      }
    }),
    activate_count_start: immediate({
      filterGte(regions, n, _0, _1, extra) {
        return [regions, (s) => s.activateCount[0] >= n];
      }
    }),
    all_corner_random: {
      samplePolicy: AllCornerRandomPolicy,
      filterEq(regions, one, course, _, extra) {
        strict(one == 1, "must be all_corner_random==1");
        const corners = course.corners.map((c) => new Region(c.start, c.start + c.length));
        return regions.rmap((r) => corners.map((c) => r.intersect(c)));
      },
      filterNeq: notSupported,
      filterLt: notSupported,
      filterLte: notSupported,
      filterGt: notSupported,
      filterGte: notSupported
    },
    always: noopImmediate,
    // NB. since skill conditions are processed before any skill activations, stats here are base stats (i.e. greens are not included)
    base_power: valueFilter((_, horse, extra) => horse.power),
    base_speed: valueFilter((_, horse, extra) => horse.speed),
    base_stamina: valueFilter((_, horse, extra) => horse.stamina),
    base_guts: valueFilter((_, horse, extra) => horse.guts),
    base_wiz: valueFilter((_, horse, extra) => horse.wisdom),
    bashin_diff_behind: noopErlangRandom(3, 2),
    bashin_diff_infront: noopErlangRandom(3, 2),
    behind_near_lane_time: noopErlangRandom(3, 2),
    // NB. at least in theory _set1 should have a slightly more early-biased distribution since it's technically easier to activate, but I don't
    // really think it makes much of a difference. Same with blocked_front vs blocked_front_continuetime I suppose.
    behind_near_lane_time_set1: noopErlangRandom(3, 2),
    blocked_all_continuetime: noopErlangRandom(3, 2),
    blocked_front: noopErlangRandom(3, 2),
    blocked_front_continuetime: erlangRandom(3, 2, {
      filterGte: shiftRegionsForwardByMinTime
    }),
    blocked_side_continuetime: erlangRandom(3, 2, {
      filterGte: shiftRegionsForwardByMinTime
    }),
    change_order_onetime: noopErlangRandom(3, 2),
    change_order_up_end_after: erlangRandom(3, 2, {
      filterGte(regions, _0, course, _1, extra) {
        const bounds = new Region(CourseHelpers.phaseStart(course.distance, 2), course.distance);
        return regions.rmap((r) => r.intersect(bounds));
      }
    }),
    change_order_up_finalcorner_after: erlangRandom(3, 2, {
      filterGte(regions, _0, course, _1, extra) {
        strict(CourseHelpers.isSortedByStart(course.corners), "course corners must be sorted by start");
        if (course.corners.length == 0) {
          return new RegionList();
        }
        const finalCornerStart = course.corners[course.corners.length - 1].start;
        const bounds = new Region(finalCornerStart, course.distance);
        return regions.rmap((r) => r.intersect(bounds));
      }
    }),
    change_order_up_middle: erlangRandom(3, 2, {
      filterGte(regions, _0, course, _1, extra) {
        const bounds = new Region(CourseHelpers.phaseStart(course.distance, 1), CourseHelpers.phaseEnd(course.distance, 1));
        return regions.rmap((r) => r.intersect(bounds));
      }
    }),
    compete_fight_count: uniformRandom({
      filterGt(regions, _0, course, _1, extra) {
        strict(CourseHelpers.isSortedByStart(course.straights), "course straights must be sorted by start");
        const lastStraight = course.straights[course.straights.length - 1];
        return regions.rmap((r) => r.intersect(lastStraight));
      }
    }),
    corner: immediate({
      filterEq(regions, cornerNum, course, _, extra) {
        strict(CourseHelpers.isSortedByStart(course.corners), "course corners must be sorted by start");
        if (cornerNum == 0) {
          let lastEnd = 0;
          const nonCorners = course.corners.map((c) => {
            const r = new Region(lastEnd, c.start);
            lastEnd = c.start + c.length;
            return r;
          });
          if (lastEnd != course.distance) {
            nonCorners.push(new Region(lastEnd, course.distance));
          }
          return regions.rmap((r) => nonCorners.map((s) => r.intersect(s)));
        } else if (course.corners.length + cornerNum >= 5) {
          const corners = [];
          for (let cornerIdx = course.corners.length + cornerNum - 5; cornerIdx >= 0; cornerIdx -= 4) {
            const corner = course.corners[cornerIdx];
            corners.push(new Region(corner.start, corner.start + corner.length));
          }
          corners.reverse();
          return regions.rmap((r) => corners.map((c) => r.intersect(c)));
        } else {
          return new RegionList();
        }
      },
      filterNeq(regions, cornerNum, course, _, extra) {
        strict(cornerNum == 0, "only supports corner!=0");
        const corners = course.corners.map((c) => new Region(c.start, c.start + c.length));
        return regions.rmap((r) => corners.map((c) => r.intersect(c)));
      }
    }),
    corner_count: valueFilter((course, _, extra) => course.corners.length),
    // FIXME this shouldn't actually be random, since in cases like corner_random==1@corner_random==2 it should sample
    // only from the first corner and not from the combined regions, so it needs its own sample policy
    // actually, that's slightly annoying to handle since corners come in back-to-back pairs, so their regions will
    // get merged by the union operation.
    // the real way to fix this is to finally allow placing multiple triggers, then each branch of an @ can simply
    // place its own trigger and the problem goes away.
    corner_random: random({
      filterEq(regions, cornerNum, course, _, extra) {
        strict(CourseHelpers.isSortedByStart(course.corners), "course corners must be sorted by start");
        if ([
          "200331",
          "200332",
          "200333",
          "200341",
          "200342",
          "200343",
          "200351",
          "200352",
          "200353",
          "200971",
          "200972",
          "201041",
          "201042",
          "201111",
          "201112",
          "201181",
          "201182",
          "201251",
          "201252",
          "201321",
          "201322",
          "201391",
          "201392",
          "201461",
          "201462"
        ].indexOf(extra.skillId) > -1) {
          if (cornerNum == 1) {
            const corner = course.corners[Math.max(course.corners.length - 4, 0)];
            const cornerBounds = new Region(corner.start, corner.start + corner.length);
            return regions.rmap((r) => r.intersect(cornerBounds));
          } else {
            return new RegionList();
          }
        }
        if (course.corners.length + cornerNum >= 5) {
          const corner = course.corners[course.corners.length + cornerNum - 5];
          const cornerBounds = new Region(corner.start, corner.start + corner.length);
          return regions.rmap((r) => r.intersect(cornerBounds));
        } else {
          return new RegionList();
        }
      }
    }),
    course_distance: valueFilter((course, _, extra) => course.distance),
    distance_diff_rate: noopImmediate,
    distance_diff_top: noopImmediate,
    distance_diff_top_float: noopImmediate,
    distance_rate: immediate({
      filterLte(regions, rate, course, _, extra) {
        const bounds = new Region(0, course.distance * rate / 100);
        return regions.rmap((r) => r.intersect(bounds));
      },
      filterGte(regions, rate, course, _, extra) {
        const bounds = new Region(course.distance * rate / 100, course.distance);
        return regions.rmap((r) => r.intersect(bounds));
      }
    }),
    distance_rate_after_random: random({
      filterEq(regions, rate, course, _, extra) {
        const bounds = new Region(course.distance * rate / 100, course.distance);
        return regions.rmap((r) => r.intersect(bounds));
      }
    }),
    distance_type: immediate({
      filterEq(regions, distanceType, course, _, extra) {
        CourseHelpers.assertIsDistanceType(distanceType);
        if (course.distanceType == distanceType) {
          return regions;
        } else {
          return new RegionList();
        }
      },
      filterNeq(regions, distanceType, course, _, extra) {
        CourseHelpers.assertIsDistanceType(distanceType);
        if (course.distanceType != distanceType) {
          return regions;
        } else {
          return new RegionList();
        }
      }
    }),
    down_slope_random: random({
      filterEq(regions, one, course, _, extra) {
        strict(one == 1, "must be down_slope_random==1");
        const slopes = course.slopes.filter((s) => s.slope < 0).map((s) => new Region(s.start, s.start + s.length));
        return regions.rmap((r) => slopes.map((s) => r.intersect(s)));
      }
    }),
    grade: valueFilter((_0, _1, extra) => extra.grade),
    ground_condition: valueFilter((_0, _1, extra) => extra.groundCondition),
    ground_type: valueFilter((course, _, extra) => course.surface),
    hp_per: immediate({
      filterLte(regions, hpPer, _0, _1, extra) {
        hpPer /= 100;
        return [regions, (s) => s.hp.hpRatioRemaining() <= hpPer];
      },
      filterGte(regions, hpPer, _0, _1, extra) {
        hpPer /= 100;
        return [regions, (s) => s.hp.hpRatioRemaining() >= hpPer];
      }
    }),
    infront_near_lane_time: noopErlangRandom(3, 2),
    is_activate_other_skill_detail: immediate({
      filterEq(regions, one, _0, _1, extra) {
        strict(one == 1, "must be is_activate_other_skill_detail==1");
        return [regions, (s) => s.usedSkills.has(extra.skillId)];
      }
    }),
    is_basis_distance: immediate({
      filterEq(regions, flag, course, _, extra) {
        strict(flag == 0 || flag == 1, "must be is_basis_distance==0 or is_basis_distance==1");
        return Math.min(course.distance % 400, 1) != flag ? regions : new RegionList();
      }
    }),
    is_badstart: immediate({
      filterEq(regions, flag, _0, _1, extra) {
        strict(flag == 0 || flag == 1, "must be is_badstart==0 or is_badstart==1");
        const f = flag ? (s) => s.startDelay > 0.08 : (s) => s.startDelay <= 0.08;
        return [regions, f];
      }
    }),
    is_behind_in: noopImmediate,
    is_dirtgrade: immediate({
      filterEq(regions, flag, course, _, extra) {
        strict(flag == 1, "must be is_dirtgrade==1");
        return [10101, 10103, 10104, 10105].indexOf(course.raceTrackId) > -1 ? regions : new RegionList();
      },
      filterNeq(regions, flag, course, _, extra) {
        strict(flag == 1, "must be is_dirtgrade!=1");
        return [10101, 10103, 10104, 10105].indexOf(course.raceTrackId) == -1 ? regions : new RegionList();
      }
    }),
    is_finalcorner: immediate({
      filterEq(regions, flag, course, _, extra) {
        strict(flag == 0 || flag == 1, "must be is_finalcorner==0 or is_finalcorner==1");
        strict(CourseHelpers.isSortedByStart(course.corners), "course corners must be sorted by start");
        if (course.corners.length == 0) {
          return new RegionList();
        }
        const finalCornerStart = course.corners[course.corners.length - 1].start;
        const bounds = flag ? new Region(finalCornerStart, course.distance) : new Region(0, finalCornerStart);
        return regions.rmap((r) => r.intersect(bounds));
      }
    }),
    is_finalcorner_laterhalf: immediate({
      filterEq(regions, one, course, _, extra) {
        strict(one == 1, "must be is_finalcorner_laterhalf==1");
        strict(CourseHelpers.isSortedByStart(course.corners), "course corners must be sorted by start");
        if (course.corners.length == 0) {
          return new RegionList();
        }
        const fc = course.corners[course.corners.length - 1];
        const bounds = new Region((fc.start + fc.start + fc.length) / 2, fc.start + fc.length);
        return regions.rmap((r) => r.intersect(bounds));
      }
    }),
    is_finalcorner_random: random({
      filterEq(regions, one, course, _, extra) {
        strict(one == 1, "must be is_finalcorner_random==1");
        strict(CourseHelpers.isSortedByStart(course.corners), "course corners must be sorted by start");
        if (course.corners.length == 0) {
          return new RegionList();
        }
        const fc = course.corners[course.corners.length - 1];
        const bounds = new Region(fc.start, fc.start + fc.length);
        return regions.rmap((r) => r.intersect(bounds));
      }
    }),
    is_hp_empty_onetime: immediate({
      filterEq(regions, one, course, _, extra) {
        strict(one == 1, "must be is_hp_empty_onetime==1");
        return [regions, (s) => !s.hp.hasRemainingHp()];
      }
    }),
    is_lastspurt: immediate({
      filterEq(regions, one, course, _, extra) {
        strict(one == 1, "must be is_lastspurt==1");
        const bounds = new Region(CourseHelpers.phaseStart(course.distance, 2), course.distance);
        return [regions.rmap((r) => r.intersect(bounds)), (s) => s.isLastSpurt];
      }
    }),
    is_last_straight: immediate({
      filterEq(regions, one, course, _, extra) {
        strict(one == 1, "must be is_last_straight_onetime==1");
        strict(CourseHelpers.isSortedByStart(course.straights), "course straights must be sorted by start");
        const lastStraight = course.straights[course.straights.length - 1];
        return regions.rmap((r) => r.intersect(lastStraight));
      }
    }),
    is_last_straight_onetime: immediate({
      filterEq(regions, one, course, _, extra) {
        strict(one == 1, "must be is_last_straight_onetime==1");
        strict(CourseHelpers.isSortedByStart(course.straights), "course straights must be sorted by start");
        const lastStraightStart = course.straights[course.straights.length - 1].start;
        const trigger = new Region(lastStraightStart, lastStraightStart + 10);
        return regions.rmap((r) => r.intersect(trigger));
      }
    }),
    is_move_lane: noopErlangRandom(5, 1),
    is_overtake: noopErlangRandom(1, 2),
    is_surrounded: noopErlangRandom(3, 2),
    is_temptation: noopImmediate,
    is_used_skill_id: immediate({
      filterEq(regions, skillId, _0, _1, extra) {
        return [regions, (s) => s.usedSkills.has("" + skillId)];
      }
    }),
    lane_type: noopImmediate,
    lastspurt: immediate({
      filterEq(regions, case_, course, _, extra) {
        let f;
        switch (case_) {
          case 1:
            f = (s) => s.isLastSpurt && s.lastSpurtTransition != -1;
            break;
          case 2:
            f = (s) => s.isLastSpurt && s.lastSpurtTransition == -1;
            break;
          case 3:
            f = (s) => !s.isLastSpurt;
            break;
          default:
            strict(1 <= case_ && case_ <= 3, "lastspurt case must be 1-3");
        }
        const bounds = new Region(CourseHelpers.phaseStart(course.distance, 2), course.distance);
        return [regions.rmap((r) => r.intersect(bounds)), f];
      }
    }),
    motivation: valueFilter((_0, _1, extra) => extra.mood + 3),
    // go from -2 to 2 to 1-5 scale
    near_count: noopErlangRandom(2, 2),
    order: orderFilter((pos, _) => pos),
    order_rate: orderFilter((rate, numUmas) => Math.round(numUmas * (rate / 100))),
    order_rate_in20_continue: orderInFilter(0.2),
    order_rate_in40_continue: orderInFilter(0.4),
    order_rate_in80_continue: orderInFilter(0.8),
    order_rate_out20_continue: orderOutFilter(0.2),
    order_rate_out40_continue: orderOutFilter(0.4),
    order_rate_out50_continue: orderOutFilter(0.5),
    order_rate_out70_continue: orderOutFilter(0.7),
    overtake_target_no_order_up_time: noopErlangRandom(3, 2),
    overtake_target_time: noopErlangRandom(3, 2),
    phase: {
      samplePolicy: ImmediatePolicy,
      filterEq(regions, phase, course, _, extra) {
        CourseHelpers.assertIsPhase(phase);
        const fudge = ["100591", "900591", "110261", "910261", "110191", "910191", "120451", "920451", "101502121", "100461"].indexOf(extra.skillId) > -1 ? 10 : 0;
        const bounds = new Region(CourseHelpers.phaseStart(course.distance, phase), CourseHelpers.phaseEnd(course.distance, phase) + fudge);
        return regions.rmap((r) => r.intersect(bounds));
      },
      filterNeq: notSupported,
      filterLt(regions, phase, course, _, extra) {
        CourseHelpers.assertIsPhase(phase);
        strict(phase > 0, "phase == 0");
        const bounds = new Region(0, CourseHelpers.phaseStart(course.distance, phase));
        return regions.rmap((r) => r.intersect(bounds));
      },
      filterLte(regions, phase, course, _, extra) {
        CourseHelpers.assertIsPhase(phase);
        const bounds = new Region(0, CourseHelpers.phaseEnd(course.distance, phase));
        return regions.rmap((r) => r.intersect(bounds));
      },
      filterGt(regions, phase, course, _, extra) {
        CourseHelpers.assertIsPhase(phase);
        strict(phase < 3, "phase > 2");
        const bounds = new Region(CourseHelpers.phaseStart(course.distance, phase + 1), course.distance);
        return regions.rmap((r) => r.intersect(bounds));
      },
      filterGte(regions, phase, course, _, extra) {
        CourseHelpers.assertIsPhase(phase);
        const bounds = new Region(CourseHelpers.phaseStart(course.distance, phase), course.distance);
        return regions.rmap((r) => r.intersect(bounds));
      }
    },
    phase_corner_random: random({
      filterEq(regions, phase, course, _, extra) {
        CourseHelpers.assertIsPhase(phase);
        const phaseStart = CourseHelpers.phaseStart(course.distance, phase);
        const phaseEnd = CourseHelpers.phaseEnd(course.distance, phase);
        const corners = course.corners.filter((c) => c.start >= phaseStart && c.start < phaseEnd || c.start + c.length >= phaseStart && c.start + c.length < phaseEnd).map((c) => new Region(Math.max(c.start, phaseStart), Math.min(c.start + c.length, phaseEnd)));
        return regions.rmap((r) => corners.map((c) => r.intersect(c)));
      }
    }),
    phase_firsthalf: immediate({
      filterEq(regions, phase, course, _, extra) {
        CourseHelpers.assertIsPhase(phase);
        const start = CourseHelpers.phaseStart(course.distance, phase);
        const end = CourseHelpers.phaseEnd(course.distance, phase);
        const bounds = new Region(start, start + (end - start) / 2);
        return regions.rmap((r) => r.intersect(bounds));
      }
    }),
    phase_firsthalf_random: random({
      filterEq(regions, phase, course, _, extra) {
        CourseHelpers.assertIsPhase(phase);
        const start = CourseHelpers.phaseStart(course.distance, phase);
        const end = CourseHelpers.phaseEnd(course.distance, phase);
        const bounds = new Region(start, start + (end - start) / 2);
        return regions.rmap((r) => r.intersect(bounds));
      }
    }),
    phase_firstquarter: immediate({
      filterEq(regions, phase, course, _, extra) {
        CourseHelpers.assertIsPhase(phase);
        const start = CourseHelpers.phaseStart(course.distance, phase);
        const end = CourseHelpers.phaseEnd(course.distance, phase);
        const bounds = new Region(start, start + (end - start) / 4);
        return regions.rmap((r) => r.intersect(bounds));
      }
    }),
    phase_firstquarter_random: random({
      filterEq(regions, phase, course, _, extra) {
        CourseHelpers.assertIsPhase(phase);
        const start = CourseHelpers.phaseStart(course.distance, phase);
        const end = CourseHelpers.phaseEnd(course.distance, phase);
        const bounds = new Region(start, start + (end - start) / 4);
        return regions.rmap((r) => r.intersect(bounds));
      }
    }),
    phase_laterhalf_random: random({
      filterEq(regions, phase, course, _, extra) {
        CourseHelpers.assertIsPhase(phase);
        const start = CourseHelpers.phaseStart(course.distance, phase);
        const end = CourseHelpers.phaseEnd(course.distance, phase);
        const bounds = new Region((start + end) / 2, end);
        return regions.rmap((r) => r.intersect(bounds));
      }
    }),
    phase_random: random({
      filterEq(regions, phase, course, _, extra) {
        CourseHelpers.assertIsPhase(phase);
        const bounds = new Region(CourseHelpers.phaseStart(course.distance, phase), CourseHelpers.phaseEnd(course.distance, phase));
        return regions.rmap((r) => r.intersect(bounds));
      }
    }),
    phase_straight_random: {
      samplePolicy: StraightRandomPolicy,
      filterEq(regions, phase, course, _, extra) {
        CourseHelpers.assertIsPhase(phase);
        const phaseBounds = new Region(CourseHelpers.phaseStart(course.distance, phase), CourseHelpers.phaseEnd(course.distance, phase));
        return regions.rmap((r) => course.straights.map((s) => r.intersect(s))).rmap((r) => r.intersect(phaseBounds));
      },
      filterNeq: notSupported,
      filterLt: notSupported,
      filterLte: notSupported,
      filterGt: notSupported,
      filterGte: notSupported
    },
    popularity: noopImmediate,
    post_number: function() {
      function gateBlock(s, numUmas) {
        const gateNumber = s.gateRoll % numUmas;
        if (gateNumber < 9)
          return gateNumber;
        else
          return 1 + (24 - gateNumber) % 8;
      }
      return immediate({
        filterEq(regions, post, _0, _1, extra) {
          return [regions, (s) => gateBlock(s, extra.numUmas || 9) == post];
        },
        filterLte(regions, post, _0, _1, extra) {
          return [regions, (s) => gateBlock(s, extra.numUmas || 9) <= post];
        },
        filterGte(regions, post, _0, _1, extra) {
          return [regions, (s) => gateBlock(s, extra.numUmas || 9) >= post];
        }
      });
    }(),
    random_lot: immediate({
      filterEq(regions, lot, _0, _1, extra) {
        return [regions, (s) => s.randomLot < lot];
      }
    }),
    remain_distance: immediate({
      filterEq(regions, remain, course, _, extra) {
        const bounds = new Region(course.distance - remain, course.distance - remain + 1);
        return regions.rmap((r) => r.intersect(bounds));
      },
      filterLte(regions, remain, course, _, extra) {
        const bounds = new Region(course.distance - remain, course.distance);
        return regions.rmap((r) => r.intersect(bounds));
      },
      filterGte(regions, remain, course, _, extra) {
        const bounds = new Region(0, course.distance - remain);
        return regions.rmap((r) => r.intersect(bounds));
      }
    }),
    rotation: valueFilter((course, _, extra) => course.turn),
    running_style: immediate({
      filterEq(regions, strategy, _, horse, extra) {
        StrategyHelpers.assertIsStrategy(strategy);
        if (StrategyHelpers.strategyMatches(horse.strategy, strategy)) {
          return regions;
        } else {
          return new RegionList();
        }
      }
    }),
    running_style_count_same: noopImmediate,
    running_style_count_same_rate: noopImmediate,
    // these are used exclusively on debuffs, in which case they only get added to /us/ from the "other" perspective, in which case
    // we actually want them to active if /our/ strategy matches the condition
    // NB. this seems kind of questionable in general. perhaps a perspective member should be added to RaceParameters.
    // also, abusing valueFilter like this only works because these conditions are used like running_style_count_nige_otherself>=1
    running_style_count_nige_otherself: valueFilter(
      (_, horse, extra) => +StrategyHelpers.strategyMatches(horse.strategy, 1 /* Nige */)
    ),
    running_style_count_senko_otherself: valueFilter(
      (_, horse, extra) => +StrategyHelpers.strategyMatches(horse.strategy, 2 /* Senkou */)
    ),
    running_style_count_sashi_otherself: valueFilter(
      (_, horse, extra) => +StrategyHelpers.strategyMatches(horse.strategy, 3 /* Sasi */)
    ),
    running_style_count_oikomi_otherself: valueFilter(
      (_, horse, extra) => +StrategyHelpers.strategyMatches(horse.strategy, 4 /* Oikomi */)
    ),
    running_style_equal_popularity_one: noopImmediate,
    running_style_temptation_count_nige: noopSectionRandom(2, 9),
    running_style_temptation_count_senko: noopSectionRandom(2, 9),
    running_style_temptation_count_sashi: noopSectionRandom(2, 9),
    running_style_temptation_count_oikomi: noopSectionRandom(2, 9),
    same_skill_horse_count: noopImmediate,
    season: valueFilter((_0, _1, extra) => extra.season),
    slope: immediate({
      filterEq(regions, slopeType, course, _, extra) {
        strict(slopeType == 0 || slopeType == 1 || slopeType == 2, "slopeType");
        strict(CourseHelpers.isSortedByStart(course.slopes), "course slopes must be sorted by slope start");
        let lastEnd = 0;
        const slopes = course.slopes.filter((s) => slopeType != 2 && s.slope > 0 || slopeType != 1 && s.slope < 0);
        const slopeR = slopeType == 0 ? slopes.map((s) => {
          const r = new Region(lastEnd, s.start);
          lastEnd = s.start + s.length;
          return r;
        }) : slopes.map((s) => new Region(s.start, s.start + s.length));
        if (slopeType == 0 && lastEnd != course.distance) {
          slopeR.push(new Region(lastEnd, course.distance));
        }
        return regions.rmap((r) => slopeR.map((s) => r.intersect(s)));
      }
    }),
    straight_front_type: immediate({
      filterEq(regions, frontType, course, _, extra) {
        strict(frontType == 1 || frontType == 2, "frontType");
        const straights = course.straights.filter((s) => s.frontType == frontType);
        return regions.rmap((r) => straights.map((s) => r.intersect(s)));
      }
    }),
    straight_random: {
      samplePolicy: StraightRandomPolicy,
      filterEq(regions, one, course, _, extra) {
        strict(one == 1, "must be straight_random==1");
        return regions.rmap((r) => course.straights.map((s) => r.intersect(s)));
      },
      filterNeq: notSupported,
      filterLt: notSupported,
      filterLte: notSupported,
      filterGt: notSupported,
      filterGte: notSupported
    },
    temptation_count: noopImmediate,
    temptation_count_behind: noopSectionRandom(2, 9),
    temptation_count_infront: noopSectionRandom(2, 9),
    time: valueFilter((_0, _1, extra) => extra.time),
    track_id: valueFilter((course, _, extra) => course.raceTrackId),
    up_slope_random: random({
      filterEq(regions, one, course, _, extra) {
        strict(one == 1, "must be up_slope_random==1");
        const slopes = course.slopes.filter((s) => s.slope > 0).map((s) => new Region(s.start, s.start + s.length));
        return regions.rmap((r) => slopes.map((s) => r.intersect(s)));
      }
    }),
    visiblehorse: noopImmediate,
    weather: valueFilter((_0, _1, extra) => extra.weather)
  });

  // vendor/kachi-uma-tools/uma-skill-tools/RaceSolverBuilder.ts
  init_ActivationSamplePolicy();

  // vendor/kachi-uma-tools/uma-skill-tools/ConditionParser.ts
  var ParseError = class extends Error {
    constructor(msg) {
      super(msg);
    }
  };
  function isId(c) {
    return "a".charCodeAt(0) <= c && c <= "z".charCodeAt(0) || "0".charCodeAt(0) <= c && c <= "9".charCodeAt(0) || c == "_".charCodeAt(0);
  }
  var IntValue = class {
    lbp = 0;
    value;
    constructor(value) {
      this.value = value;
    }
    led(state, left) {
      throw new ParseError("unexpected integer literal");
    }
    nud(state) {
      return { type: 0 /* Int */, value: this.value };
    }
  };
  function getParser(conditions = Conditions, operators = {
    and: AndOperator,
    // this is really stupid
    or: OrOperator,
    eq: EqOperator,
    neq: NeqOperator,
    lt: LtOperator,
    lte: LteOperator,
    gt: GtOperator,
    gte: GteOperator
  }) {
    const Eof = Object.freeze({
      lbp: 0,
      led: (state, left) => {
        throw new ParseError("unexpected eof");
      },
      nud: (state) => {
        throw new ParseError("unexpected eof");
      }
    });
    class Identifier {
      lbp = 0;
      value;
      constructor(value) {
        this.value = value;
      }
      led(state, left) {
        throw new ParseError("unexpected identifier");
      }
      nud(state) {
        return { type: 1 /* Cond */, cond: conditions[this.value] };
      }
    }
    class CmpOp {
      constructor(lbp, opclass) {
        this.lbp = lbp;
        this.opclass = opclass;
      }
      led(state, left) {
        if (left.type != 1 /* Cond */)
          throw new ParseError("expected condition on left hand side of comparison");
        const right = expression(state, this.lbp);
        if (right.type != 0 /* Int */)
          throw new ParseError("expected number on right hand side of comparison");
        return { type: 2 /* Op */, op: new this.opclass(left.cond, right.value) };
      }
      nud(state) {
        throw new ParseError("expected expression");
      }
    }
    class LogicalOp {
      constructor(lbp, opclass) {
        this.lbp = lbp;
        this.opclass = opclass;
      }
      led(state, left) {
        if (left.type != 2 /* Op */)
          throw new ParseError("expected comparison on left hand side of operator");
        const right = expression(state, this.lbp);
        if (right.type != 2 /* Op */)
          throw new ParseError("expected comparison on right hand side of operator");
        return { type: 2 /* Op */, op: new this.opclass(left.op, right.op) };
      }
      nud(state) {
        throw new ParseError("expected expression");
      }
    }
    const OperatorEq = Object.freeze(new CmpOp(30, operators.eq));
    const OperatorNeq = Object.freeze(new CmpOp(30, operators.neq));
    const OperatorLt = Object.freeze(new CmpOp(30, operators.lt));
    const OperatorLte = Object.freeze(new CmpOp(30, operators.lte));
    const OperatorGt = Object.freeze(new CmpOp(30, operators.gt));
    const OperatorGte = Object.freeze(new CmpOp(30, operators.gte));
    const OperatorAnd = Object.freeze(new LogicalOp(20, operators.and));
    const OperatorOr = Object.freeze(new LogicalOp(10, operators.or));
    function* tokenize(s) {
      var i = 0;
      while (i < s.length) {
        var c = s.charCodeAt(i);
        if ("0".charCodeAt(0) <= c && c <= "9".charCodeAt(0)) {
          var n = 0;
          while ("0".charCodeAt(0) <= c && c <= "9".charCodeAt(0)) {
            n *= 10;
            n += c - "0".charCodeAt(0);
            c = s.charCodeAt(++i);
          }
          yield new IntValue(n);
        } else if (isId(c)) {
          var idstart = i;
          while (isId(c)) {
            c = s.charCodeAt(++i);
          }
          yield new Identifier(s.slice(idstart, i));
        } else
          switch (s[i]) {
            case "=":
              if (s[++i] != "=")
                throw new ParseError("expected =");
              ++i;
              yield OperatorEq;
              break;
            case "!":
              if (s[++i] != "=")
                throw new ParseError("expected =");
              ++i;
              yield OperatorNeq;
              break;
            case "<":
              if (s[++i] == "=") {
                ++i;
                yield OperatorLte;
              } else {
                yield OperatorLt;
              }
              break;
            case ">":
              if (s[++i] == "=") {
                ++i;
                yield OperatorGte;
              } else {
                yield OperatorGt;
              }
              break;
            case "@":
              yield OperatorOr;
              ++i;
              break;
            case "&":
              yield OperatorAnd;
              ++i;
              break;
            default:
              throw new ParseError("invalid character");
          }
      }
      return Eof;
    }
    function parseAny(tokens) {
      const state = { current: Eof, next: tokens.next().value, tokens };
      return expression(state, 0);
    }
    function parse(tokens) {
      const node = parseAny(tokens);
      if (node.type != 2 /* Op */) {
        throw new ParseError("expected comparison or operator");
      }
      return node.op;
    }
    function expression(state, rbp) {
      state.current = state.next;
      state.next = state.tokens.next().value;
      var left = state.current.nud(state);
      while (rbp < state.next.lbp) {
        state.current = state.next;
        state.next = state.tokens.next().value;
        left = state.current.led(state, left);
      }
      return left;
    }
    return { tokenize, parse, parseAny };
  }

  // vendor/kachi-uma-tools/uma-skill-tools/ApproximateConditions.ts
  var ApproximateStartContinue = class {
    constructor(name, startRate, continuationRate) {
      this.name = name;
      this.startRate = startRate;
      this.continuationRate = continuationRate;
    }
    get valueOnStart() {
      return 0;
    }
    update(state, currentValue) {
      const rng = state.simulation.specialConditionRng;
      if (currentValue === 0) {
        return rng.random() < this.startRate ? 1 : 0;
      } else {
        return rng.random() < this.continuationRate ? 1 : 0;
      }
    }
  };
  var ApproximateMultiCondition = class {
    constructor(name, conditions, valueOnStart = 0) {
      this.name = name;
      this.conditions = conditions;
      this.valueOnStart = valueOnStart;
    }
    update(state, currentValue) {
      let activeCondition = null;
      let fallbackCondition = null;
      for (const entry of this.conditions) {
        if (entry.predicate === null) {
          fallbackCondition = entry.condition;
        } else if (entry.predicate(state)) {
          activeCondition = entry.condition;
          break;
        }
      }
      const condition = activeCondition || fallbackCondition;
      if (!condition) {
        return currentValue;
      }
      const rng = state.simulation.specialConditionRng;
      if (currentValue === 0) {
        return rng.random() < condition.startRate ? 1 : 0;
      } else {
        return rng.random() < condition.continuationRate ? 1 : 0;
      }
    }
  };

  // vendor/kachi-uma-tools/uma-skill-tools/SpecialConditions.ts
  function createBlockedSideCondition() {
    const conditions = [
      {
        condition: new ApproximateStartContinue("Outer lane", 0, 0),
        predicate: (state) => {
          const sim = state.simulation;
          const section = Math.floor(sim.pos / sim.sectionLength);
          return section >= 1 && section <= 3 && sim.currentLane > 3 * sim.course.horseLane;
        }
      },
      {
        condition: new ApproximateStartContinue("Early race", 0.1, 0.85),
        predicate: (state) => state.simulation.phase === 0
      },
      {
        condition: new ApproximateStartContinue("Mid race", 0.08, 0.75),
        predicate: (state) => state.simulation.phase === 1
      },
      {
        condition: new ApproximateStartContinue("Other", 0.07, 0.5),
        predicate: null
      }
    ];
    return new ApproximateMultiCondition(
      "blocked_side",
      conditions,
      1
    );
  }
  function createOvertakeCondition() {
    const conditions = [
      {
        condition: new ApproximateStartContinue("\u9003\u3052", 0.05, 0.5),
        predicate: (state) => {
          return state.simulation.horse.strategy === 1 /* Nige */;
        }
      },
      {
        condition: new ApproximateStartContinue("\u5148\u884C", 0.15, 0.55),
        predicate: (state) => {
          return state.simulation.horse.strategy === 2 /* Senkou */;
        }
      },
      {
        condition: new ApproximateStartContinue("\u305D\u306E\u4ED6", 0.2, 0.6),
        predicate: null
      }
    ];
    return new ApproximateMultiCondition(
      "overtake",
      conditions
    );
  }

  // vendor/kachi-uma-tools/uma-skill-tools/RaceSolver.ts
  if (false)
    global.CC_GLOBAL = false;
  var Speed;
  ((Speed2) => {
    Speed2.StrategyPhaseCoefficient = Object.freeze([
      [],
      // strategies start numbered at 1
      [1, 0.98, 0.962],
      [0.978, 0.991, 0.975],
      [0.938, 0.998, 0.994],
      [0.931, 1, 1],
      [1.063, 0.962, 0.95]
    ].map((a) => Object.freeze(a)));
    Speed2.DistanceProficiencyModifier = Object.freeze([1.05, 1, 0.9, 0.8, 0.6, 0.4, 0.2, 0.1]);
  })(Speed || (Speed = {}));
  function baseSpeed(course) {
    return 20 - (course.distance - 2e3) / 1e3;
  }
  function baseTargetSpeed(horse, course, phase) {
    return baseSpeed(course) * Speed.StrategyPhaseCoefficient[horse.strategy][phase] + +(phase == 2) * Math.sqrt(500 * horse.speed) * Speed.DistanceProficiencyModifier[horse.distanceAptitude] * 2e-3;
  }
  function lastSpurtSpeed(horse, course) {
    let v = (baseTargetSpeed(horse, course, 2) + 0.01 * baseSpeed(course)) * 1.05 + Math.sqrt(500 * horse.speed) * Speed.DistanceProficiencyModifier[horse.distanceAptitude] * 2e-3;
    v += Math.pow(450 * horse.guts, 0.597) * 1e-4;
    return v;
  }
  var Acceleration;
  ((Acceleration2) => {
    Acceleration2.StrategyPhaseCoefficient = Object.freeze([
      [],
      [1, 1, 0.996],
      [0.985, 1, 0.996],
      [0.975, 1, 1],
      [0.945, 1, 0.997],
      [1.17, 0.94, 0.956]
    ].map((a) => Object.freeze(a)));
    Acceleration2.GroundTypeProficiencyModifier = Object.freeze([1.05, 1, 0.9, 0.8, 0.7, 0.5, 0.3, 0.1]);
    Acceleration2.DistanceProficiencyModifier = Object.freeze([1, 1, 1, 1, 1, 0.6, 0.5, 0.4]);
  })(Acceleration || (Acceleration = {}));
  var BaseAccel = 6e-4;
  var UphillBaseAccel = 4e-4;
  function baseAccel(baseAccel2, horse, phase) {
    return baseAccel2 * Math.sqrt(500 * horse.power) * Acceleration.StrategyPhaseCoefficient[horse.strategy][phase] * Acceleration.GroundTypeProficiencyModifier[horse.surfaceAptitude] * Acceleration.DistanceProficiencyModifier[horse.distanceAptitude];
  }
  var PhaseDeceleration = [-1.2, -0.8, -1];
  var PositionKeep;
  ((PositionKeep2) => {
    PositionKeep2.BaseMinimumThreshold = Object.freeze([0, 0, 3, 6.5, 7.5]);
    PositionKeep2.BaseMaximumThreshold = Object.freeze([0, 0, 5, 7, 8]);
    function courseFactor(distance) {
      return 8e-4 * (distance - 1e3) + 1;
    }
    PositionKeep2.courseFactor = courseFactor;
    function minThreshold(strategy, distance) {
      return PositionKeep2.BaseMinimumThreshold[strategy] * (strategy == 2 /* Senkou */ ? 1 : courseFactor(distance));
    }
    PositionKeep2.minThreshold = minThreshold;
    function maxThreshold(strategy, distance) {
      return PositionKeep2.BaseMaximumThreshold[strategy] * courseFactor(distance);
    }
    PositionKeep2.maxThreshold = maxThreshold;
  })(PositionKeep || (PositionKeep = {}));
  var Timer = class {
    constructor(t) {
      this.t = t;
    }
  };
  var CompensatedAccumulator = class {
    constructor(acc, err = 0) {
      this.acc = acc;
      this.err = err;
    }
    add(n) {
      const t = this.acc + n;
      if (Math.abs(this.acc) >= Math.abs(n)) {
        this.err += this.acc - t + n;
      } else {
        this.err += n - t + this.acc;
      }
      this.acc = t;
    }
  };
  var SkillType = /* @__PURE__ */ ((SkillType2) => {
    SkillType2[SkillType2["Noop"] = 0] = "Noop";
    SkillType2[SkillType2["SpeedUp"] = 1] = "SpeedUp";
    SkillType2[SkillType2["StaminaUp"] = 2] = "StaminaUp";
    SkillType2[SkillType2["PowerUp"] = 3] = "PowerUp";
    SkillType2[SkillType2["GutsUp"] = 4] = "GutsUp";
    SkillType2[SkillType2["WisdomUp"] = 5] = "WisdomUp";
    SkillType2[SkillType2["Recovery"] = 9] = "Recovery";
    SkillType2[SkillType2["MultiplyStartDelay"] = 10] = "MultiplyStartDelay";
    SkillType2[SkillType2["SetStartDelay"] = 14] = "SetStartDelay";
    SkillType2[SkillType2["CurrentSpeed"] = 21] = "CurrentSpeed";
    SkillType2[SkillType2["CurrentSpeedWithNaturalDeceleration"] = 22] = "CurrentSpeedWithNaturalDeceleration";
    SkillType2[SkillType2["TargetSpeed"] = 27] = "TargetSpeed";
    SkillType2[SkillType2["LaneMovementSpeed"] = 28] = "LaneMovementSpeed";
    SkillType2[SkillType2["Accel"] = 31] = "Accel";
    SkillType2[SkillType2["ChangeLane"] = 35] = "ChangeLane";
    SkillType2[SkillType2["ActivateRandomGold"] = 37] = "ActivateRandomGold";
    SkillType2[SkillType2["ExtendEvolvedDuration"] = 42] = "ExtendEvolvedDuration";
    return SkillType2;
  })(SkillType || {});
  function noop2(x) {
  }
  var RaceSolver = class {
    accumulatetime;
    pos;
    minSpeed;
    currentSpeed;
    targetSpeed;
    accel;
    baseTargetSpeed;
    lastSpurtSpeed;
    lastSpurtTransition;
    sectionModifier;
    baseAccel;
    horse;
    course;
    hp;
    rng;
    syncRng;
    gorosiRng;
    rushedRng;
    downhillRng;
    wisdomRollRng;
    posKeepRng;
    laneMovementRng;
    specialConditionRng;
    competeFightRng;
    timers;
    startDash;
    startDelay;
    startDelayAccumulator;
    gateRoll;
    randomLot;
    isLastSpurt;
    phase;
    nextPhaseTransition;
    activeTargetSpeedSkills;
    activeCurrentSpeedSkills;
    activeAccelSkills;
    activeLaneMovementSkills;
    activeChangeLaneSkills;
    pendingSkills;
    pendingRemoval;
    usedSkills;
    nHills;
    hillIdx;
    slopePer;
    hillStart;
    hillEnd;
    activateCount;
    activateCountHeal;
    onSkillActivate;
    onSkillDeactivate;
    sectionLength;
    umas;
    isPacer;
    pacerOverride;
    posKeepMinThreshold;
    posKeepMaxThreshold;
    posKeepCooldown;
    posKeepNextTimer;
    posKeepExitPosition;
    posKeepExitDistance;
    posKeepEnd;
    positionKeepState;
    posKeepMode;
    posKeepSpeedCoef;
    posKeepStrategy;
    mode;
    pacer;
    skillWisdomCheck;
    rushedKakari;
    // Rushed state
    isRushed;
    hasBeenRushed;
    // Track if horse has already been rushed this race (can only happen once)
    rushedSection;
    // Which section (2-9) the rushed state activates in
    rushedEnterPosition;
    // Position where rushed state should activate
    rushedTimer;
    // Tracks time in rushed state
    rushedMaxDuration;
    // Maximum duration (12s + extensions)
    rushedActivations;
    // Track [start, end] positions for UI
    positionKeepActivations;
    // Track [start, end, state] positions for UI
    speedUpProbability;
    // 0-100, probability of entering speed-up mode
    //downhill mode
    isDownhillMode;
    downhillTimer;
    downhillActivations;
    // Compete Fight
    canCompeteFight;
    competeFight;
    competeFightStart;
    competeFightEnd;
    competeFightTimer;
    competeFightEnabled;
    duelingRates;
    // Lead Competition
    leadCompetitionEnabled;
    leadCompetition;
    leadCompetitionStart;
    leadCompetitionEnd;
    leadCompetitionTimer;
    // lane movement..........
    laneMovementEnabled;
    currentLane;
    targetLane;
    laneChangeSpeed;
    extraMoveLane;
    forceInSpeed;
    firstUmaInLateRace;
    hpDied;
    hpDiedPosition;
    fullSpurt;
    nonFullSpurtVelocityDiff;
    nonFullSpurtDelayDistance;
    modifiers;
    conditionTimer;
    conditionValues = /* @__PURE__ */ new Map();
    conditions = /* @__PURE__ */ new Map();
    constructor(params) {
      this.horse = Object.assign({}, params.horse);
      this.course = params.course;
      this.hp = params.hp;
      this.rng = params.rng;
      this.pendingSkills = params.skills.slice();
      this.pendingRemoval = /* @__PURE__ */ new Set();
      this.usedSkills = /* @__PURE__ */ new Set();
      this.syncRng = new Rule30CARng(this.rng.int32());
      this.gorosiRng = new Rule30CARng(this.rng.int32());
      this.rushedRng = new Rule30CARng(this.rng.int32());
      this.wisdomRollRng = new Rule30CARng(this.rng.int32());
      this.posKeepRng = new Rule30CARng(this.rng.int32());
      this.laneMovementRng = new Rule30CARng(this.rng.int32());
      this.specialConditionRng = new Rule30CARng(this.rng.int32());
      this.competeFightRng = new Rule30CARng(this.rng.int32());
      this.timers = [];
      this.conditionTimer = this.getNewTimer(-1);
      this.accumulatetime = this.getNewTimer();
      this.gateRoll = this.rng.uniform(12252240);
      this.randomLot = this.rng.uniform(100);
      this.phase = 0;
      this.nextPhaseTransition = CourseHelpers.phaseStart(this.course.distance, 1);
      this.activeTargetSpeedSkills = [];
      this.activeCurrentSpeedSkills = [];
      this.activeAccelSkills = [];
      this.activeLaneMovementSkills = [];
      this.activeChangeLaneSkills = [];
      this.activateCount = [0, 0, 0];
      this.activateCountHeal = 0;
      this.onSkillActivate = params.onSkillActivate || noop2;
      this.onSkillDeactivate = params.onSkillDeactivate || noop2;
      this.sectionLength = this.course.distance / 24;
      this.posKeepMinThreshold = PositionKeep.minThreshold(this.horse.strategy, this.course.distance);
      this.posKeepMaxThreshold = PositionKeep.maxThreshold(this.horse.strategy, this.course.distance);
      this.posKeepNextTimer = this.getNewTimer();
      this.positionKeepState = 0 /* None */;
      this.posKeepMode = params.posKeepMode || 0 /* None */;
      this.posKeepStrategy = this.horse.strategy;
      this.mode = params.mode;
      this.skillWisdomCheck = params.skillWisdomCheck !== false;
      this.rushedKakari = params.rushedKakari !== false;
      this.posKeepEnd = this.sectionLength * (this.mode === "compare" ? 10 : 3);
      this.posKeepSpeedCoef = 1;
      this.isPacer = params.isPacer || false;
      this.pacerOverride = false;
      this.umas = [];
      this.pacer = null;
      this.speedUpProbability = params.speedUpProbability != null ? params.speedUpProbability : 100;
      this.isRushed = false;
      this.hasBeenRushed = false;
      this.rushedSection = -1;
      this.rushedEnterPosition = -1;
      this.rushedTimer = this.getNewTimer();
      this.rushedMaxDuration = 12;
      this.isDownhillMode = false;
      this.downhillActivations = [];
      this.rushedActivations = [];
      this.positionKeepActivations = [];
      this.firstUmaInLateRace = false;
      this.hpDied = false;
      this.hpDiedPosition = null;
      this.fullSpurt = false;
      this.nonFullSpurtVelocityDiff = null;
      this.nonFullSpurtDelayDistance = null;
      if (this.rushedKakari) {
        this.initRushedState();
      }
      this.competeFightEnabled = params.competeFight !== false;
      this.duelingRates = params.duelingRates || null;
      this.canCompeteFight = null;
      this.competeFight = false;
      this.competeFightStart = null;
      this.competeFightEnd = null;
      this.competeFightTimer = this.getNewTimer();
      this.leadCompetitionEnabled = params.leadCompetition !== false;
      this.leadCompetition = false;
      this.leadCompetitionStart = null;
      this.leadCompetitionEnd = null;
      this.leadCompetitionTimer = this.getNewTimer();
      this.laneMovementEnabled = params.laneMovement !== false;
      const gateNumberRaw = this.gateRoll % 9;
      const gateNumber = gateNumberRaw < 9 ? gateNumberRaw : 1 + (24 - gateNumberRaw) % 8;
      const initialLane = gateNumber * this.course.horseLane;
      this.currentLane = initialLane;
      this.targetLane = initialLane;
      this.laneChangeSpeed = 0;
      this.extraMoveLane = -1;
      this.forceInSpeed = 0;
      this.modifiers = {
        targetSpeed: new CompensatedAccumulator(0),
        currentSpeed: new CompensatedAccumulator(0),
        accel: new CompensatedAccumulator(0),
        oneFrameAccel: 0,
        specialSkillDurationScaling: 1
      };
      this.startDelay = 0.1 * this.rng.random();
      this.pos = 0;
      this.accel = 0;
      this.currentSpeed = 3;
      this.targetSpeed = 0.85 * baseSpeed(this.course);
      this.processSkillActivations();
      this.minSpeed = 0.85 * baseSpeed(this.course) + Math.sqrt(200 * this.horse.guts) * 1e-3;
      this.startDash = true;
      this.modifiers.accel.add(24);
      this.initHills();
      this.startDelayAccumulator = this.startDelay;
      this.baseTargetSpeed = [0, 1, 2].map((phase) => baseTargetSpeed(this.horse, this.course, phase));
      this.lastSpurtSpeed = lastSpurtSpeed(this.horse, this.course);
      this.lastSpurtTransition = -1;
      this.sectionModifier = Array.from({ length: 24 }, () => {
        const max = this.horse.wisdom / 5500 * Math.log10(this.horse.wisdom * 0.1);
        const factor = (max - 0.65 + this.wisdomRollRng.random() * 0.65) / 100;
        return baseSpeed(this.course) * factor;
      });
      this.sectionModifier.push(0);
      this.hp.init(this.horse);
      this.baseAccel = [0, 1, 2, 0, 1, 2].map((phase, i) => baseAccel(i > 2 ? UphillBaseAccel : BaseAccel, this.horse, phase));
      this.registerCondition("blocked_side", createBlockedSideCondition());
      this.registerCondition("overtake", createOvertakeCondition());
    }
    initUmas(umas) {
      this.umas = [...umas.filter((uma) => uma != null), this];
    }
    initHills() {
      strict(CourseHelpers.isSortedByStart(this.course.slopes), "slopes must be sorted by start location");
      this.nHills = this.course.slopes.length;
      this.hillStart = this.course.slopes.map((s) => s.start).reverse();
      this.hillEnd = this.course.slopes.map((s) => s.start + s.length).reverse();
      this.hillIdx = -1;
      this.downhillRng = this.course.slopes.map((_) => new Rule30CARng(this.rng.int32()));
      this.downhillTimer = this.getNewTimer();
      if (this.hillStart.length > 0 && this.hillStart[this.hillStart.length - 1] == 0) {
        this.hillIdx = 0;
        this.slopePer = this.course.slopes[0].slope;
        this.downhillTimer.t = 0;
        this.downhillCheck(this.downhillRng[0].random());
        this.hillStart.pop();
      } else {
        this.slopePer = 0;
      }
    }
    getNewTimer(t = 0) {
      const tm = new Timer(t);
      this.timers.push(tm);
      return tm;
    }
    initRushedState() {
      const wisdomStat = this.horse.wisdom;
      const rushedChance = Math.pow(6.5 / Math.log10(0.1 * wisdomStat + 1), 2) / 100;
      const hasSelfControl = this.pendingSkills.some((s) => s.skillId === "202161");
      const finalRushedChance = Math.max(0, rushedChance - (hasSelfControl ? 0.03 : 0));
      if (this.rushedRng.random() < finalRushedChance) {
        this.rushedSection = 2 + this.rushedRng.uniform(8);
        this.rushedEnterPosition = this.sectionLength * this.rushedSection;
      }
    }
    updateRushedState() {
      if (this.rushedSection >= 0 && !this.isRushed && !this.hasBeenRushed && this.pos >= this.rushedEnterPosition) {
        this.isRushed = true;
        this.hasBeenRushed = true;
        this.rushedTimer.t = 0;
        this.rushedActivations.push([this.pos, -1]);
      }
      if (this.isRushed) {
        if (this.rushedTimer.t > 0 && Math.floor(this.rushedTimer.t / 3) > Math.floor((this.rushedTimer.t - 0.017) / 3)) {
          if (this.rushedRng.random() < 0.55) {
            this.endRushedState();
            return;
          }
        }
        if (this.rushedTimer.t >= this.rushedMaxDuration) {
          this.endRushedState();
        }
      }
    }
    endRushedState() {
      this.isRushed = false;
      if (this.rushedActivations.length > 0) {
        const lastIdx = this.rushedActivations.length - 1;
        if (this.rushedActivations[lastIdx][1] === -1) {
          this.rushedActivations[lastIdx][1] = this.pos;
        }
      }
    }
    getMaxStartDashSpeed() {
      return Math.min(this.targetSpeed, 0.85 * baseSpeed(this.course));
    }
    logVelocityData(dt) {
      console.log("frame: ", this.accumulatetime.t);
      console.log("current speed: ", this.currentSpeed);
      console.log("accel: ", this.accel);
      console.log("dist:", this.pos);
      console.log("--------------------------------");
    }
    step(dt) {
      let dtAfterDelay = dt;
      this.timers.forEach((tm) => tm.t += dt);
      if (this.conditionTimer.t >= 0) {
        this.tickConditions();
        this.conditionTimer.t = -1;
      }
      if (this.startDelayAccumulator > 0) {
        this.startDelayAccumulator -= dt;
        if (this.startDelayAccumulator > 0) {
          return;
        }
      }
      this.updateHills();
      this.updatePhase();
      this.updateRushedState();
      this.processSkillActivations();
      this.applyPositionKeepStates();
      this.updatePositionKeepCoefficient();
      this.updateCompeteFight();
      this.updateLeadCompetition();
      this.updateLastSpurtState();
      this.updateTargetSpeed();
      this.applyForces();
      if (this.laneMovementEnabled) {
        this.applyLaneMovement();
      }
      let newSpeed = void 0;
      if (this.currentSpeed <= this.targetSpeed) {
        newSpeed = Math.min(this.currentSpeed + this.accel * dt, this.targetSpeed);
      } else {
        newSpeed = Math.max(this.currentSpeed + this.accel * dt, this.targetSpeed);
      }
      if (this.startDash && newSpeed > this.getMaxStartDashSpeed()) {
        newSpeed = this.getMaxStartDashSpeed();
      }
      if (!this.startDash && this.currentSpeed < this.minSpeed) {
        newSpeed = this.minSpeed;
      }
      this.currentSpeed = newSpeed;
      const displacement = this.currentSpeed + this.modifiers.currentSpeed.acc + this.modifiers.currentSpeed.err;
      if (this.startDelayAccumulator < 0) {
        dtAfterDelay = Math.abs(this.startDelayAccumulator);
        this.startDelayAccumulator = 0;
      }
      this.pos += displacement * dtAfterDelay;
      this.hp.tick(this, dt);
      if (!this.hp.hasRemainingHp() && !this.hpDied) {
        this.hpDied = true;
        this.hpDiedPosition = this.course.distance - this.pos;
      }
      if (this.startDash && this.currentSpeed >= 0.85 * baseSpeed(this.course)) {
        this.startDash = false;
        this.modifiers.accel.add(-24);
      }
      this.modifiers.oneFrameAccel = 0;
    }
    applyLaneMovement() {
      const currentLane = this.currentLane;
      const sideBlocked = this.getConditionValue("blocked_side") === 1;
      const overtake = this.getConditionValue("overtake") === 1;
      if (this.extraMoveLane < 0 && this.isAfterFinalCornerOrInFinalStraight()) {
        this.extraMoveLane = Math.min(currentLane / 0.1, this.course.maxLaneDistance) * 0.5 + this.laneMovementRng.random() * 0.1;
      }
      if (this.activeChangeLaneSkills.length > 0) {
        this.targetLane = 9.5 * this.course.horseLane;
      } else if (overtake) {
        this.targetLane = Math.max(this.targetLane, this.course.horseLane, this.extraMoveLane);
      } else if (!this.hp.hasRemainingHp()) {
        this.targetLane = currentLane;
      } else if (this.positionKeepState === 2 /* PaceDown */) {
        this.targetLane = 0.18;
      } else if (this.extraMoveLane > currentLane) {
        this.targetLane = this.extraMoveLane;
      } else if (this.phase <= 1 && !sideBlocked) {
        this.targetLane = Math.max(0, currentLane - 0.05);
      } else {
        this.targetLane = currentLane;
      }
      if (sideBlocked && this.targetLane < currentLane || Math.abs(this.targetLane - currentLane) < 1e-5) {
        this.laneChangeSpeed = 0;
      } else {
        let targetSpeed = 0.02 * (0.3 + 1e-3 * this.horse.power);
        if (this.pos < this.course.moveLanePoint) {
          targetSpeed *= 1 + currentLane / this.course.maxLaneDistance * 0.05;
        }
        this.laneChangeSpeed = Math.min(this.laneChangeSpeed + this.course.laneChangeAccelerationPerFrame, targetSpeed);
        let actualSpeed = Math.min(this.laneChangeSpeed + this.activeLaneMovementSkills.reduce((sum, skill) => sum + skill.modifier, 0), 0.6);
        if (this.targetLane > currentLane) {
          this.currentLane = Math.min(this.targetLane, currentLane + actualSpeed);
        } else {
          this.currentLane = Math.max(this.targetLane, currentLane - actualSpeed * (1 + currentLane));
        }
      }
    }
    // Slightly scuffed way of ensuring all umas use the same pacemaker
    // in compare.ts, call .getPacer() on any uma (doesn't matter which)
    // and then call .updatePacer(result) on all umas to update pacer reference
    updatePacer(pacemaker) {
      this.pacer = pacemaker;
    }
    getPacer() {
      for (const strategy of [5 /* Oonige */, 1 /* Nige */]) {
        var umas = this.umas.filter((uma2) => uma2.posKeepStrategy === strategy);
        if (umas.length > 0) {
          var uma = umas.reduce((max, uma2) => {
            return uma2.pos > max.pos ? uma2 : max;
          }, umas[0]);
          return uma;
        }
      }
      var pacerOverrideUma = this.umas.find((uma2) => uma2.pacerOverride);
      if (pacerOverrideUma) {
        return pacerOverrideUma;
      }
      for (const strategy of [2 /* Senkou */, 3 /* Sasi */, 4 /* Oikomi */]) {
        var umas = this.umas.filter((uma2) => StrategyHelpers.strategyMatches(uma2.posKeepStrategy, strategy));
        if (umas.length > 0) {
          var uma = umas.reduce((max, uma2) => {
            return uma2.pos > max.pos ? uma2 : max;
          }, umas[0]);
          uma.pacerOverride = true;
          uma.posKeepStrategy = 1 /* Nige */;
          return uma;
        }
      }
      var pacer = this.umas.find((uma2) => uma2.isPacer);
      if (pacer) {
        pacer.posKeepStrategy = 1 /* Nige */;
        return pacer;
      }
    }
    getUmaByDistanceDescending() {
      return this.umas.sort((a, b) => b.pos - a.pos);
    }
    isOnlyFrontRunner() {
      var frontRunners = this.umas.filter((uma) => StrategyHelpers.strategyMatches(uma.posKeepStrategy, 1 /* Nige */));
      return frontRunners.length === 1 && frontRunners[0] === this;
    }
    // In Virtual Pacemaker mode, we care about the effects of position keep and the way
    // umas react during poskeep based on their wit
    //
    // In Approximate mode, we don't really care about poskeep - it's just a way to give out
    // PDM/PUM early-race to mimic what actually happens in game so we limit poskeep to 5 sections
    // and use synced rng to make skill comparison possible.
    speedUpOvertakeWitCheck() {
      return this.posKeepRng.random() < 0.2 * Math.log10(0.1 * this.horse.wisdom);
    }
    paceUpWitCheck() {
      return this.posKeepRng.random() < 0.15 * Math.log10(0.1 * this.horse.wisdom);
    }
    applyPositionKeepStates() {
      if (this.pos >= this.posKeepEnd || this.posKeepMode === 0 /* None */) {
        if (this.positionKeepState !== 0 /* None */ && this.positionKeepActivations.length > 0) {
          this.positionKeepActivations[this.positionKeepActivations.length - 1][1] = this.pos;
        }
        this.positionKeepState = 0 /* None */;
        return;
      }
      if (!this.pacer) {
        return;
      }
      var pacer = this.pacer;
      var behind = pacer.pos - this.pos;
      var myStrategy = this.posKeepStrategy;
      switch (this.positionKeepState) {
        case 0 /* None */:
          if (this.posKeepNextTimer.t < 0) {
            return;
          }
          if (StrategyHelpers.strategyMatches(myStrategy, 1 /* Nige */)) {
            if (pacer === this) {
              var umas = this.getUmaByDistanceDescending();
              var secondPlaceUma = umas[1];
              var distanceAhead = pacer.pos - secondPlaceUma.pos;
              let threshold = myStrategy === 5 /* Oonige */ ? 17.5 : 4.5;
              if (this.posKeepNextTimer.t < 0) {
                return;
              }
              if (distanceAhead < threshold && this.speedUpOvertakeWitCheck()) {
                this.positionKeepActivations.push([this.pos, 0, 3 /* SpeedUp */]);
                this.positionKeepState = 3 /* SpeedUp */;
                this.posKeepExitPosition = this.pos + Math.floor(this.sectionLength);
              }
            } else if (this.speedUpOvertakeWitCheck()) {
              this.positionKeepState = 4 /* Overtake */;
              this.positionKeepActivations.push([this.pos, 0, 4 /* Overtake */]);
            }
          } else {
            if (behind > this.posKeepMaxThreshold) {
              if (this.paceUpWitCheck()) {
                this.positionKeepState = 1 /* PaceUp */;
                this.positionKeepActivations.push([this.pos, 0, 1 /* PaceUp */]);
                this.posKeepExitDistance = this.posKeepRng.random() * (this.posKeepMaxThreshold - this.posKeepMinThreshold) + this.posKeepMinThreshold;
              }
            } else if (behind < this.posKeepMinThreshold) {
              if (this.activeTargetSpeedSkills.length == 0 && this.activeCurrentSpeedSkills.length == 0) {
                this.positionKeepState = 2 /* PaceDown */;
                this.positionKeepActivations.push([this.pos, 0, 2 /* PaceDown */]);
                this.posKeepExitDistance = this.posKeepRng.random() * (this.posKeepMaxThreshold - this.posKeepMinThreshold) + this.posKeepMinThreshold;
              }
            }
          }
          if (this.positionKeepState == 0 /* None */) {
            this.posKeepNextTimer.t = -2;
          } else {
            this.posKeepExitPosition = this.pos + Math.floor(this.sectionLength);
          }
          break;
        case 3 /* SpeedUp */:
          if (this.pos >= this.posKeepExitPosition) {
            this.positionKeepState = 0 /* None */;
            this.positionKeepActivations[this.positionKeepActivations.length - 1][1] = this.pos;
            this.posKeepNextTimer.t = -3;
          } else if (pacer == this) {
            var umas = this.getUmaByDistanceDescending();
            var secondPlaceUma = umas[1];
            var distanceAhead = pacer.pos - secondPlaceUma.pos;
            let threshold = myStrategy === 5 /* Oonige */ ? 17.5 : 4.5;
            if (distanceAhead >= threshold) {
              this.positionKeepState = 0 /* None */;
              this.positionKeepActivations[this.positionKeepActivations.length - 1][1] = this.pos;
              this.posKeepNextTimer.t = -3;
            }
          }
          break;
        case 4 /* Overtake */:
          if (this.pos >= this.posKeepExitPosition) {
            this.positionKeepState = 0 /* None */;
            this.positionKeepActivations[this.positionKeepActivations.length - 1][1] = this.pos;
            this.posKeepNextTimer.t = -3;
          } else if (pacer == this) {
            var umas = this.getUmaByDistanceDescending();
            var secondPlaceUma = umas[1];
            var distanceAhead = this.pos - secondPlaceUma.pos;
            let threshold = myStrategy === 5 /* Oonige */ ? 27.5 : 10;
            if (distanceAhead >= threshold) {
              this.positionKeepState = 0 /* None */;
              this.positionKeepActivations[this.positionKeepActivations.length - 1][1] = this.pos;
              this.posKeepNextTimer.t = -3;
            }
          }
          break;
        case 1 /* PaceUp */:
          if (this.pos >= this.posKeepExitPosition) {
            this.positionKeepState = 0 /* None */;
            this.positionKeepActivations[this.positionKeepActivations.length - 1][1] = this.pos;
            this.posKeepNextTimer.t = -3;
          } else {
            if (behind < this.posKeepExitDistance) {
              this.positionKeepState = 0 /* None */;
              this.positionKeepActivations[this.positionKeepActivations.length - 1][1] = this.pos;
              this.posKeepNextTimer.t = -3;
            }
          }
          break;
        case 2 /* PaceDown */:
          if (this.pos >= this.posKeepExitPosition) {
            this.positionKeepState = 0 /* None */;
            this.positionKeepActivations[this.positionKeepActivations.length - 1][1] = this.pos;
            this.posKeepNextTimer.t = -3;
          } else {
            if (behind > this.posKeepExitDistance || this.activeTargetSpeedSkills.length > 0 || this.activeCurrentSpeedSkills.length > 0) {
              this.positionKeepState = 0 /* None */;
              this.positionKeepActivations[this.positionKeepActivations.length - 1][1] = this.pos;
              this.posKeepNextTimer.t = -3;
            }
          }
          break;
        default:
          break;
      }
    }
    updatePositionKeepCoefficient() {
      switch (this.positionKeepState) {
        case 3 /* SpeedUp */:
          this.posKeepSpeedCoef = 1.04;
          break;
        case 4 /* Overtake */:
          this.posKeepSpeedCoef = 1.05;
          break;
        case 1 /* PaceUp */:
          this.posKeepSpeedCoef = 1.04;
          break;
        case 2 /* PaceDown */:
          this.posKeepSpeedCoef = 0.915;
          break;
        default:
          this.posKeepSpeedCoef = 1;
          break;
      }
    }
    isOnFinalStraight() {
      const lastStraight = this.course.straights[this.course.straights.length - 1];
      return this.pos >= lastStraight.start && this.pos <= lastStraight.end;
    }
    isAfterFinalCorner() {
      const finalCornerStart = this.course.corners.length > 0 ? this.course.corners[this.course.corners.length - 1].start : Infinity;
      return this.pos >= finalCornerStart;
    }
    isAfterFinalCornerOrInFinalStraight() {
      return this.isAfterFinalCorner() || this.isOnFinalStraight();
    }
    updateCompeteFight() {
      if (!this.competeFightEnabled) {
        return;
      }
      if (this.competeFight) {
        if (this.hp.hpRatioRemaining() <= 0.05) {
          this.competeFight = false;
          this.competeFightEnd = this.pos;
        }
        return;
      }
      if (StrategyHelpers.strategyMatches(this.posKeepStrategy, 1 /* Nige */)) {
        return;
      }
      if (this.hp.hpRatioRemaining() < 0.15 || !this.isOnFinalStraight()) {
        return;
      }
      if (this.canCompeteFight === null) {
        if (this.duelingRates) {
          let rate = 0;
          if (this.posKeepStrategy === 5 /* Oonige */) {
            rate = this.duelingRates.runaway;
          } else if (this.posKeepStrategy === 1 /* Nige */) {
            rate = this.duelingRates.frontRunner;
          } else if (this.posKeepStrategy === 2 /* Senkou */) {
            rate = this.duelingRates.paceChaser;
          } else if (this.posKeepStrategy === 3 /* Sasi */) {
            rate = this.duelingRates.lateSurger;
          } else if (this.posKeepStrategy === 4 /* Oikomi */) {
            rate = this.duelingRates.endCloser;
          }
          this.canCompeteFight = this.competeFightRng.random() < rate / 100;
          this.competeFightTimer.t = 0;
        } else {
          this.canCompeteFight = false;
        }
      }
      if (!this.canCompeteFight) {
        return;
      }
      if (this.competeFightTimer.t >= 1) {
        if (this.competeFightRng.random() <= 0.4) {
          this.competeFight = true;
          this.competeFightStart = this.pos;
        } else {
          this.competeFightTimer.t = 0;
        }
      }
    }
    updateLeadCompetition() {
      if (!this.leadCompetitionEnabled) {
        return;
      }
      if (this.leadCompetition) {
        let leadCompeteDuration = Math.pow(700 * this.horse.guts, 0.5) * 0.012;
        if (this.leadCompetitionTimer.t >= leadCompeteDuration || this.pos >= this.leadCompetitionEnd) {
          this.leadCompetition = false;
          this.leadCompetitionEnd = this.pos;
        }
      }
      if (this.leadCompetitionStart !== null) {
        return;
      }
      if (this.pos >= 150 && this.pos <= Math.floor(this.sectionLength * 5) && StrategyHelpers.strategyMatches(this.posKeepStrategy, 1 /* Nige */)) {
        let otherUmas = this.umas.filter((u) => u.posKeepStrategy === this.posKeepStrategy);
        let distanceGap = this.posKeepStrategy === 1 /* Nige */ ? 3.75 : 5;
        let umasWithinGap = otherUmas.filter((u) => Math.abs(u.pos - this.pos) <= distanceGap);
        if (umasWithinGap.length >= 2) {
          for (let uma of umasWithinGap) {
            uma.leadCompetitionTimer.t = 0;
            uma.leadCompetition = true;
            uma.leadCompetitionStart = uma.pos;
            uma.leadCompetitionEnd = uma.pos + Math.floor(this.sectionLength * 8);
          }
        }
      }
    }
    updatefirstUmaInLateRace() {
      let existingFirstPlaceUma = this.umas.find((u) => u.firstUmaInLateRace);
      if (existingFirstPlaceUma) {
        return;
      }
      let sortedUmas = this.getUmaByDistanceDescending();
      let firstPlaceUma = sortedUmas[0];
      if (firstPlaceUma.pos < this.course.distance * 2 / 3) {
        return;
      }
      const firstPlacePos = Math.round(firstPlaceUma.pos * 100) / 100;
      const tiedUmas = [];
      for (let uma of sortedUmas) {
        const umaPos = Math.round(uma.pos * 100) / 100;
        if (umaPos === firstPlacePos) {
          tiedUmas.push(uma);
        } else {
          break;
        }
      }
      const selectedUma = tiedUmas[this.syncRng.uniform(tiedUmas.length)];
      selectedUma.firstUmaInLateRace = true;
    }
    updateLastSpurtState(force = false) {
      if (this.isLastSpurt || this.phase < 2)
        return;
      if (this.lastSpurtTransition == -1 || force) {
        const initialLastSpurtSpeed = this.lastSpurtSpeed;
        const v = this.hp.getLastSpurtPair(this, this.lastSpurtSpeed, this.baseTargetSpeed[2]);
        this.lastSpurtTransition = v[0];
        this.lastSpurtSpeed = v[1];
        if (this.hp.isMaxSpurt && this.hp.isMaxSpurt()) {
          this.fullSpurt = true;
        } else {
          this.nonFullSpurtVelocityDiff = this.lastSpurtSpeed - initialLastSpurtSpeed;
          this.nonFullSpurtDelayDistance = this.lastSpurtTransition >= 0 ? this.lastSpurtTransition - this.course.distance * 2 / 3 : null;
        }
      }
      if (this.pos >= this.lastSpurtTransition) {
        this.isLastSpurt = true;
      }
    }
    updateTargetSpeed() {
      if (!this.hp.hasRemainingHp()) {
        this.targetSpeed = this.minSpeed;
      } else if (this.isLastSpurt) {
        this.targetSpeed = this.lastSpurtSpeed;
      } else {
        this.targetSpeed = this.baseTargetSpeed[this.phase] * this.posKeepSpeedCoef;
        this.targetSpeed += this.sectionModifier[Math.floor(this.pos / this.sectionLength)];
      }
      this.targetSpeed += this.modifiers.targetSpeed.acc + this.modifiers.targetSpeed.err;
      if (this.isDownhillMode) {
        this.targetSpeed += 0.3 + this.slopePer / 1e5;
      } else if (this.hillIdx != -1 && this.slopePer > 0) {
        this.targetSpeed -= this.slopePer / 1e4 * 200 / this.horse.power;
        this.targetSpeed = Math.max(this.targetSpeed, this.minSpeed);
      }
      if (this.competeFight) {
        this.targetSpeed += Math.pow(200 * this.horse.guts, 0.708) * 1e-4;
      }
      if (this.leadCompetition) {
        this.targetSpeed += Math.pow(500 * this.horse.guts, 0.6) * 1e-4;
      }
      if (this.laneChangeSpeed > 0 && this.activeLaneMovementSkills.length > 0) {
        const moveLaneModifier = Math.sqrt(2e-4 * this.horse.power);
        this.targetSpeed += moveLaneModifier;
      }
    }
    applyForces() {
      if (!this.hp.hasRemainingHp()) {
        this.accel = -1.2;
        return;
      }
      if (this.currentSpeed > this.targetSpeed) {
        this.accel = this.positionKeepState === 2 /* PaceDown */ ? -0.5 : PhaseDeceleration[this.phase];
        return;
      }
      this.accel = this.baseAccel[+(this.slopePer > 0) * 3 + this.phase];
      this.accel += this.modifiers.accel.acc + this.modifiers.accel.err;
      if (this.competeFight) {
        this.accel += Math.pow(160 * this.horse.guts, 0.59) * 1e-4;
      }
    }
    downhillCheck(roll) {
      if (this.slopePer < 0 && roll < this.horse.wisdom * 4e-4) {
        this.downhillActivations.push([this.pos, this.pos]);
        this.isDownhillMode = true;
      }
    }
    updateHills() {
      if (this.hillIdx == -1 && this.hillStart.length > 0 && this.pos >= this.hillStart[this.hillStart.length - 1]) {
        this.hillIdx = this.nHills - this.hillStart.length;
        this.slopePer = this.course.slopes[this.hillIdx].slope;
        this.downhillTimer.t = 0;
        this.downhillCheck(this.downhillRng[this.hillIdx].random());
        this.hillStart.pop();
      } else if (this.hillIdx != -1 && this.hillEnd.length > 0 && this.pos > this.hillEnd[this.hillEnd.length - 1]) {
        this.hillIdx = -1;
        this.slopePer = 0;
        this.hillEnd.pop();
        if (this.isDownhillMode)
          this.downhillActivations[this.downhillActivations.length - 1][1] = this.pos;
        this.isDownhillMode = false;
      }
      if (this.downhillTimer.t >= 1 && this.hillIdx != -1) {
        const roll = this.downhillRng[this.hillIdx].random();
        if (this.isDownhillMode && roll > 0.8) {
          if (this.isDownhillMode)
            this.downhillActivations[this.downhillActivations.length - 1][1] = this.pos;
          this.isDownhillMode = false;
        } else if (!this.isDownhillMode) {
          this.downhillCheck(roll);
        }
        this.downhillTimer.t = 0;
      }
    }
    updatePhase() {
      if (this.pos >= this.nextPhaseTransition && this.phase < 2) {
        ++this.phase;
        this.nextPhaseTransition = CourseHelpers.phaseStart(this.course.distance, this.phase + 1);
      }
    }
    processSkillActivations() {
      for (let i = this.activeTargetSpeedSkills.length; --i >= 0; ) {
        const s = this.activeTargetSpeedSkills[i];
        if (s.durationTimer.t >= 0) {
          this.activeTargetSpeedSkills.splice(i, 1);
          this.modifiers.targetSpeed.add(-s.modifier);
          this.onSkillDeactivate(this, s.skillId, s.perspective);
        }
      }
      for (let i = this.activeCurrentSpeedSkills.length; --i >= 0; ) {
        const s = this.activeCurrentSpeedSkills[i];
        if (s.durationTimer.t >= 0) {
          this.activeCurrentSpeedSkills.splice(i, 1);
          this.modifiers.currentSpeed.add(-s.modifier);
          if (s.naturalDeceleration) {
            this.modifiers.oneFrameAccel += s.modifier;
          }
          this.onSkillDeactivate(this, s.skillId, s.perspective);
        }
      }
      for (let i = this.activeAccelSkills.length; --i >= 0; ) {
        const s = this.activeAccelSkills[i];
        if (s.durationTimer.t >= 0) {
          this.activeAccelSkills.splice(i, 1);
          this.modifiers.accel.add(-s.modifier);
          this.onSkillDeactivate(this, s.skillId, s.perspective);
        }
      }
      for (let i = this.activeLaneMovementSkills.length; --i >= 0; ) {
        const s = this.activeLaneMovementSkills[i];
        if (s.durationTimer.t >= 0) {
          this.activeLaneMovementSkills.splice(i, 1);
          this.onSkillDeactivate(this, s.skillId, s.perspective);
        }
      }
      for (let i = this.activeChangeLaneSkills.length; --i >= 0; ) {
        const s = this.activeChangeLaneSkills[i];
        if (s.durationTimer.t >= 0) {
          this.activeChangeLaneSkills.splice(i, 1);
          this.onSkillDeactivate(this, s.skillId, s.perspective);
        }
      }
      for (let i = this.pendingSkills.length; --i >= 0; ) {
        const s = this.pendingSkills[i];
        if (this.pos >= s.trigger.end || this.pendingRemoval.has(s.skillId)) {
          this.pendingSkills.splice(i, 1);
          this.pendingRemoval.delete(s.skillId);
        } else if (this.pos >= s.trigger.start && s.extraCondition(this)) {
          if (!this.shouldSkipWisdomCheck(s) && !this.checkWisdomForSkill(s)) {
            this.pendingSkills.splice(i, 1);
          } else {
            this.activateSkill(s);
            this.pendingSkills.splice(i, 1);
          }
        }
      }
    }
    checkWisdomForSkill(skill) {
      let rngRoll = this.wisdomRollRng.random();
      const wisdom = skill.perspective === 2 /* Other */ && skill.originWisdom !== void 0 ? skill.originWisdom : this.horse.rawWisdom;
      let wisdomCheck = Math.max(100 - 9e3 / wisdom, 20) * 0.01;
      return rngRoll <= wisdomCheck;
    }
    shouldSkipWisdomCheck(skill) {
      if (!this.skillWisdomCheck) {
        return true;
      }
      if (skill.effects.length > 0 && skill.effects[0].type >= 1 && skill.effects[0].type <= 5) {
        return true;
      }
      if (skill.rarity === 3 /* Unique */) {
        return true;
      }
      return false;
    }
    activateSkill(s) {
      s.effects.sort((a, b) => +(a.type == 42) - +(b.type == 42)).forEach((ef) => {
        const scaledDuration = ef.baseDuration * (this.course.distance / 1e3) * (s.rarity == 6 /* Evolution */ ? this.modifiers.specialSkillDurationScaling : 1);
        switch (ef.type) {
          case 0 /* Noop */:
            break;
          case 1 /* SpeedUp */:
            this.horse.speed = Math.max(this.horse.speed + ef.modifier, 1);
            break;
          case 2 /* StaminaUp */:
            this.horse.stamina = Math.max(this.horse.stamina + ef.modifier, 1);
            this.horse.rawStamina = Math.max(this.horse.rawStamina + ef.modifier, 1);
            break;
          case 3 /* PowerUp */:
            this.horse.power = Math.max(this.horse.power + ef.modifier, 1);
            break;
          case 4 /* GutsUp */:
            this.horse.guts = Math.max(this.horse.guts + ef.modifier, 1);
            break;
          case 5 /* WisdomUp */:
            this.horse.wisdom = Math.max(this.horse.wisdom + ef.modifier, 1);
            break;
          case 10 /* MultiplyStartDelay */:
            this.startDelay *= ef.modifier;
            break;
          case 14 /* SetStartDelay */:
            this.startDelay = ef.modifier;
            break;
          case 27 /* TargetSpeed */:
            this.modifiers.targetSpeed.add(ef.modifier);
            this.activeTargetSpeedSkills.push({ skillId: s.skillId, perspective: s.perspective, durationTimer: this.getNewTimer(-scaledDuration), modifier: ef.modifier });
            break;
          case 31 /* Accel */:
            this.modifiers.accel.add(ef.modifier);
            this.activeAccelSkills.push({ skillId: s.skillId, perspective: s.perspective, durationTimer: this.getNewTimer(-scaledDuration), modifier: ef.modifier });
            break;
          case 28 /* LaneMovementSpeed */:
            this.activeLaneMovementSkills.push({ skillId: s.skillId, perspective: s.perspective, durationTimer: this.getNewTimer(-scaledDuration), modifier: ef.modifier });
            break;
          case 21 /* CurrentSpeed */:
          case 22 /* CurrentSpeedWithNaturalDeceleration */:
            this.modifiers.currentSpeed.add(ef.modifier);
            this.activeCurrentSpeedSkills.push({
              skillId: s.skillId,
              perspective: s.perspective,
              durationTimer: this.getNewTimer(-scaledDuration),
              modifier: ef.modifier,
              naturalDeceleration: ef.type == 22 /* CurrentSpeedWithNaturalDeceleration */
            });
            break;
          case 9 /* Recovery */:
            if (s.perspective == 1 /* Self */)
              ++this.activateCountHeal;
            this.hp.recover(ef.modifier);
            if (this.phase >= 2 && !this.isLastSpurt) {
              this.updateLastSpurtState(true);
            }
            break;
          case 37 /* ActivateRandomGold */:
            this.doActivateRandomGold(ef.modifier);
            break;
          case 42 /* ExtendEvolvedDuration */:
            this.modifiers.specialSkillDurationScaling = ef.modifier;
            break;
          case 35 /* ChangeLane */:
            this.activeChangeLaneSkills.push({ skillId: s.skillId, perspective: s.perspective, durationTimer: this.getNewTimer(-scaledDuration), modifier: ef.modifier });
            break;
        }
      });
      if (s.perspective == 1 /* Self */)
        ++this.activateCount[this.phase];
      this.usedSkills.add(s.skillId);
      this.onSkillActivate(this, s.skillId, s.perspective);
    }
    doActivateRandomGold(ngolds) {
      const goldIndices = this.pendingSkills.reduce((acc, skill, i) => {
        if ((skill.rarity == 2 /* Gold */ || skill.rarity == 6 /* Evolution */) && skill.effects.every((ef) => ef.type > 5 /* WisdomUp */))
          acc.push(i);
        return acc;
      }, []);
      for (let i = goldIndices.length; --i >= 0; ) {
        const j = this.gorosiRng.uniform(i + 1);
        [goldIndices[i], goldIndices[j]] = [goldIndices[j], goldIndices[i]];
      }
      for (let i = 0; i < Math.min(ngolds, goldIndices.length); ++i) {
        const s = this.pendingSkills[goldIndices[i]];
        this.activateSkill(s);
        this.pendingRemoval.add(s.skillId);
      }
    }
    // deactivate any skills that haven't finished their durations yet (intended to be called at the end of a simulation, when a skill
    // might have activated towards the end of the race and the race finished before the skill's duration)
    cleanup() {
      const callDeactivateHook = (s) => {
        this.onSkillDeactivate(this, s.skillId, s.perspective);
      };
      this.activeTargetSpeedSkills.forEach(callDeactivateHook);
      this.activeCurrentSpeedSkills.forEach(callDeactivateHook);
      this.activeAccelSkills.forEach(callDeactivateHook);
      this.activeLaneMovementSkills.forEach(callDeactivateHook);
      this.activeChangeLaneSkills.forEach(callDeactivateHook);
    }
    registerCondition(name, condition) {
      this.conditions.set(name, condition);
      if (!this.conditionValues.has(name)) {
        this.conditionValues.set(name, condition.valueOnStart);
      }
    }
    getConditionValue(name) {
      if (!this.conditionValues.has(name)) {
        if (this.conditions.has(name)) {
          const condition = this.conditions.get(name);
          return condition.valueOnStart;
        }
        throw new Error(`Condition "${name}" is not registered`);
      }
      return this.conditionValues.get(name);
    }
    tickConditions() {
      const state = {
        simulation: this
      };
      for (const [name, condition] of this.conditions.entries()) {
        const currentValue = this.conditionValues.get(name) ?? condition.valueOnStart;
        const newValue = condition.update(state, currentValue);
        this.conditionValues.set(name, newValue);
      }
    }
  };

  // vendor/kachi-uma-tools/uma-skill-tools/HpPolicy.ts
  var NoopHpPolicy = {
    init(_) {
    },
    tick(_0, _1) {
    },
    hasRemainingHp() {
      return true;
    },
    hpRatioRemaining() {
      return 1;
    },
    recover(_) {
    },
    getLastSpurtPair(_0, maxSpeed, _1) {
      return [-1, maxSpeed];
    }
  };
  var HpStrategyCoefficient = Object.freeze([0, 0.95, 0.89, 1, 0.995, 0.86]);
  var HpConsumptionGroundModifier = Object.freeze([
    [],
    [0, 1, 1, 1.02, 1.02],
    [0, 1, 1, 1.01, 1.02]
  ].map((o) => Object.freeze(o)));
  var GameHpPolicy = class {
    distance;
    baseSpeed;
    maxHp;
    hp;
    groundModifier;
    gutsModifier;
    subparAcceptChance;
    rng;
    achievedMaxSpurt = false;
    constructor(course, ground, rng) {
      this.distance = course.distance;
      this.baseSpeed = 20 - (course.distance - 2e3) / 1e3;
      this.groundModifier = HpConsumptionGroundModifier[course.surface][ground];
      this.rng = rng;
      this.maxHp = 1;
      this.hp = 1;
      this.achievedMaxSpurt = false;
    }
    init(horse) {
      this.maxHp = 0.8 * HpStrategyCoefficient[horse.strategy] * horse.stamina + this.distance;
      this.hp = this.maxHp;
      this.gutsModifier = 1 + 200 / Math.sqrt(600 * horse.guts);
      this.subparAcceptChance = Math.round((15 + 0.05 * horse.wisdom) * 1e3);
      this.achievedMaxSpurt = false;
    }
    getStatusModifier(state) {
      let modifier = 1;
      if (state.isDownhillMode) {
        modifier *= 0.4;
      }
      if (state.leadCompetition) {
        const isOonige = state.posKeepStrategy === 5 /* Oonige */;
        if (state.isRushed) {
          modifier *= isOonige ? 7.7 : 3.6;
        } else {
          modifier *= isOonige ? 3.5 : 1.4;
        }
      } else if (state.isRushed) {
        modifier *= 1.6;
      }
      if (state.positionKeepState === 2 /* PaceDown */) {
        modifier *= 0.6;
      }
      return modifier;
    }
    hpPerSecond(state, velocity) {
      const gutsModifier = state.phase >= 2 ? this.gutsModifier : 1;
      return 20 * Math.pow(velocity - this.baseSpeed + 12, 2) / 144 * this.getStatusModifier(state) * this.groundModifier * gutsModifier;
    }
    tick(state, dt) {
      this.hp -= this.hpPerSecond(state, state.currentSpeed) * dt;
    }
    hasRemainingHp() {
      return this.hp > 0;
    }
    hpRatioRemaining() {
      return Math.max(0, this.hp / this.maxHp);
    }
    recover(modifier) {
      this.hp = Math.min(this.maxHp, this.hp + this.maxHp * modifier);
    }
    getLastSpurtPair(state, maxSpeed, baseTargetSpeed2) {
      const maxDist = this.distance - CourseHelpers.phaseStart(this.distance, 2);
      const s = (maxDist - 60) / maxSpeed;
      const lastleg = { phase: 2, positionKeepState: 0 /* None */, leadCompetition: false, posKeepStrategy: state.posKeepStrategy };
      const hpNeeded = this.hpPerSecond(lastleg, maxSpeed) * s;
      if (this.hp >= hpNeeded) {
        if (!this.achievedMaxSpurt) {
          this.achievedMaxSpurt = true;
        }
        return [-1, maxSpeed];
      }
      const candidates = [];
      const remainDistance = this.distance - 60 - state.pos;
      const statusModifier = this.getStatusModifier(lastleg);
      for (let speed = maxSpeed - 0.1; speed >= baseTargetSpeed2; speed -= 0.1) {
        const spurtDuration = Math.min(
          remainDistance / speed,
          Math.max(
            0,
            (baseTargetSpeed2 * this.hp - this.hpPerSecond(lastleg, baseTargetSpeed2) * remainDistance) / (baseTargetSpeed2 * this.hpPerSecond(lastleg, speed) - this.hpPerSecond(lastleg, baseTargetSpeed2) * speed)
          )
        );
        const spurtDistance = spurtDuration * speed;
        candidates.push([this.distance - spurtDistance - 60, speed]);
      }
      candidates.sort((a, b) => (a[0] - state.pos) / baseTargetSpeed2 + (this.distance - a[0]) / a[1] - ((b[0] - state.pos) / baseTargetSpeed2 + (this.distance - b[0]) / b[1]));
      for (let i = 0; i < candidates.length; ++i) {
        if (this.rng.uniform(1e5) <= this.subparAcceptChance) {
          return candidates[i];
        }
      }
      return candidates[candidates.length - 1];
    }
    /**
     * Check if max spurt was achieved
     */
    isMaxSpurt() {
      return this.achievedMaxSpurt;
    }
  };

  // vendor/kachi-uma-tools/umalator-global/skill_data.json
  var skill_data_default = { "100011": { alternatives: [{ baseDuration: 5e4, condition: "phase>=2&order>=1&order_rate<=50&change_order_onetime<0", effects: [{ modifier: 3500, target: 1, type: 22 }, { modifier: 1e3, target: 1, type: 31 }], precondition: "" }], rarity: 5 }, "100021": { alternatives: [{ baseDuration: 5e4, condition: "distance_rate>=50&order==1&bashin_diff_behind>=1", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 5 }, "100031": { alternatives: [{ baseDuration: 5e4, condition: "is_finalcorner==1&corner==0", effects: [{ modifier: 4500, target: 1, type: 27 }], precondition: "phase>=2&order<=3&bashin_diff_infront<=1&is_overtake==1" }], rarity: 5 }, "100041": { alternatives: [{ baseDuration: 4e4, condition: "is_finalcorner==1&order<=5&order_rate<=50", effects: [{ modifier: 4e3, target: 1, type: 31 }], precondition: "" }], rarity: 5 }, "100051": { alternatives: [{ baseDuration: 5e4, condition: "remain_distance<=300", effects: [{ modifier: 4500, target: 1, type: 27 }], precondition: "phase>=2&order_rate<=40&behind_near_lane_time_set1>=1" }], rarity: 5 }, "100061": { alternatives: [{ baseDuration: 5e4, condition: "order>=2&order<=5&order_rate<=50&remain_distance<=201&remain_distance>=199", effects: [{ modifier: 4500, target: 1, type: 27 }], precondition: "" }], rarity: 5 }, "100071": { alternatives: [{ baseDuration: 6e4, condition: "distance_rate>=50&distance_rate<=60&order_rate>50", effects: [{ modifier: 2500, target: 1, type: 27 }], precondition: "" }], rarity: 4 }, "100081": { alternatives: [{ baseDuration: 5e4, condition: "order>=3&order_rate<=50&remain_distance<=200&bashin_diff_infront<=1@order>=3&order_rate<=50&remain_distance<=200&bashin_diff_behind<=1", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 4 }, "100091": { alternatives: [{ baseDuration: 5e4, condition: "distance_rate>=50&order==1&bashin_diff_behind<=1@distance_rate>=50&order==2&is_overtake==1", effects: [{ modifier: 2500, target: 1, type: 27 }, { modifier: 3e3, target: 1, type: 31 }], precondition: "" }], rarity: 4 }, "100101": { alternatives: [{ baseDuration: 4e4, condition: "is_finalcorner_laterhalf==1&corner!=0&order>=3&order_rate<=40", effects: [{ modifier: 4e3, target: 1, type: 31 }], precondition: "" }], rarity: 5 }, "100111": { alternatives: [{ baseDuration: 5e4, condition: "is_finalcorner==1&corner==0&change_order_onetime<0&order>=3", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 4 }, "100121": { alternatives: [{ baseDuration: 5e4, condition: "is_finalcorner==1&corner==0", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "is_finalcorner==1&is_behind_in==1&change_order_onetime<0&order_rate>=40" }], rarity: 5 }, "100131": { alternatives: [{ baseDuration: 5e4, condition: "is_finalcorner==1&corner!=0&distance_diff_rate<=30&distance_type==4&lastspurt==2", effects: [{ modifier: 4500, target: 1, type: 27 }], precondition: "" }, { baseDuration: 5e4, condition: "is_finalcorner==1&corner!=0&distance_diff_rate<=30", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 5 }, "100141": { alternatives: [{ baseDuration: 5e4, condition: "is_finalcorner==1&corner==0&hp_per>=30&order<=2", effects: [{ modifier: 2500, target: 1, type: 27 }, { modifier: 3e3, target: 1, type: 31 }], precondition: "" }], rarity: 4 }, "100151": { alternatives: [{ baseDuration: 5e4, condition: "is_finalcorner==1&bashin_diff_behind<=1&order<=4@is_finalcorner==1&bashin_diff_infront<=1&order<=4", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 5 }, "100161": { alternatives: [{ baseDuration: 5e4, condition: "is_finalcorner==1&order>=2&order_rate<=75&is_behind_in==1&change_order_onetime<0", effects: [{ modifier: 4500, target: 1, type: 27 }], precondition: "phase==1&blocked_side_continuetime>=2" }, { baseDuration: 5e4, condition: "is_finalcorner==1&order>=2&order_rate<=75&is_behind_in==1&change_order_onetime<0", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 5 }, "100171": { alternatives: [{ baseDuration: 5e4, condition: "is_finalcorner==1&change_order_up_end_after>=3&corner==0", effects: [{ modifier: 4500, target: 1, type: 27 }], precondition: "" }], rarity: 5 }, "100181": { alternatives: [{ baseDuration: 5e4, condition: "is_finalcorner==1&corner!=0&order>=3&change_order_onetime<0", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 4 }, "100191": { alternatives: [{ baseDuration: 5e4, condition: "change_order_up_end_after>=2", effects: [{ modifier: 3500, target: 1, type: 27 }, { modifier: 350, target: 1, type: 28 }], precondition: "" }], rarity: 5 }, "100201": { alternatives: [{ baseDuration: 4e4, condition: "phase>=2&corner!=0&order==1", effects: [{ modifier: 4e3, target: 1, type: 31 }], precondition: "" }], rarity: 5 }, "100211": { alternatives: [{ baseDuration: 5e4, condition: "distance_rate>=50&corner==0&order_rate>=70&order_rate<=75&is_overtake==1@distance_rate>=50&corner==0&order_rate<=30&order_rate>=20", effects: [{ modifier: 3500, target: 1, type: 27 }, { modifier: 1e3, target: 1, type: 31 }], precondition: "" }], rarity: 5 }, "100221": { alternatives: [{ baseDuration: 5e4, condition: "distance_rate>=50&order>=2&order_rate<=40&blocked_side_continuetime>=2", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 5 }, "100231": { alternatives: [{ baseDuration: 5e4, condition: "phase>=2&is_finalcorner==1&order<=4&temptation_count==0", effects: [{ modifier: 4500, target: 1, type: 27 }], precondition: "" }, { baseDuration: 5e4, condition: "phase>=2&is_finalcorner==1&order<=4", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 5 }, "100241": { alternatives: [{ baseDuration: 5e4, condition: "is_finalcorner==1&blocked_side_continuetime>=2&order<=3", effects: [{ modifier: 2500, target: 1, type: 27 }, { modifier: 3e3, target: 1, type: 31 }], precondition: "" }], rarity: 4 }, "100251": { alternatives: [{ baseDuration: 6e4, condition: "distance_rate>=50&order_rate>=40&order_rate<=70", effects: [{ modifier: 2500, target: 1, type: 27 }, { modifier: -500, target: 9, type: 21 }], precondition: "" }], rarity: 5 }, "100261": { alternatives: [{ baseDuration: 5e4, condition: "is_badstart==0&order<=3&is_finalcorner==1&corner==0&order_rate_in20_continue==1", effects: [{ modifier: 4500, target: 1, type: 27 }], precondition: "" }, { baseDuration: 5e4, condition: "is_badstart==0&order<=3&is_finalcorner==1&corner==0", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 5 }, "100271": { alternatives: [{ baseDuration: 4e4, condition: "phase>=2&corner!=0&order_rate>=65&order_rate<=70", effects: [{ modifier: 4e3, target: 1, type: 31 }], precondition: "" }], rarity: 4 }, "100281": { alternatives: [{ baseDuration: 5e4, condition: "distance_rate>=45&distance_rate<=60&order>=3&order_rate<=50&hp_per<=70", effects: [{ modifier: 2500, target: 1, type: 27 }, { modifier: 3e3, target: 1, type: 31 }], precondition: "" }], rarity: 5 }, "100301": { alternatives: [{ baseDuration: 5e4, condition: "is_finalcorner==1&corner==0", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "phase>=2&order<=4&change_order_onetime<0" }], rarity: 5 }, "100311": { alternatives: [{ baseDuration: 5e4, condition: "remain_distance<=299&remain_distance>=295&order<=2&slope==0@remain_distance<=299&remain_distance>=295&order<=2&slope==2", effects: [{ modifier: 4500, target: 1, type: 27 }], precondition: "remain_distance<=305&remain_distance>=300&slope==1" }, { baseDuration: 5e4, condition: "remain_distance<=299&remain_distance>=295&order<=2", effects: [{ modifier: 2500, target: 1, type: 27 }], precondition: "" }], rarity: 5 }, "100321": { alternatives: [{ baseDuration: 4e4, condition: "distance_rate>=50&corner!=0&order>=3&order_rate<=40", effects: [{ modifier: 550, target: 1, type: 9 }, { modifier: 2500, target: 1, type: 27 }], precondition: "" }], rarity: 4 }, "100331": { alternatives: [{ baseDuration: 5e4, condition: "is_finalcorner==1&corner==0&distance_diff_top>=5&order_rate<80", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }, { baseDuration: 5e4, condition: "is_finalcorner==1&corner==0&distance_diff_top>=5&order_rate>=80", effects: [{ modifier: 4500, target: 1, type: 27 }], precondition: "" }], rarity: 5 }, "100341": { alternatives: [{ baseDuration: 5e4, condition: "compete_fight_count>0", effects: [{ modifier: 4500, target: 1, type: 27 }], precondition: "distance_rate>=50&order_rate_out40_continue==1" }], rarity: 5 }, "100351": { alternatives: [{ baseDuration: 5e4, condition: "is_finalcorner==1&corner==0&order<=5", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "is_finalcorner==1&blocked_side_continuetime>=2" }], rarity: 4 }, "100361": { alternatives: [{ baseDuration: 5e4, condition: "is_finalcorner==1&corner==0", effects: [{ modifier: 3500, target: 1, type: 27 }, { modifier: 350, target: 1, type: 28 }], precondition: "is_finalcorner==1&order_rate>=40&order_rate<=75&lane_type==0" }], rarity: 5 }, "100371": { alternatives: [{ baseDuration: 5e4, condition: "temptation_count==0&order>=3&is_finalcorner==1&corner==0&order_rate_in80_continue==1&order_rate_out40_continue==1", effects: [{ modifier: 4500, target: 1, type: 27 }], precondition: "" }, { baseDuration: 5e4, condition: "temptation_count==0&order>=3&is_finalcorner==1&corner==0", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 5 }, "100381": { alternatives: [{ baseDuration: 5e4, condition: "distance_rate>=50&distance_rate<=65&order>=2&order_rate<=40&change_order_onetime<0", effects: [{ modifier: 2500, target: 1, type: 27 }, { modifier: 3e3, target: 1, type: 31 }], precondition: "" }], rarity: 5 }, "100391": { alternatives: [{ baseDuration: 5e4, condition: "is_finalcorner==1&corner==0&blocked_side_continuetime>=2", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 5 }, "100401": { alternatives: [{ baseDuration: 6e4, condition: "distance_rate>=50&order_rate>=40&order_rate<=70&is_overtake==1", effects: [{ modifier: 3e3, target: 1, type: 31 }], precondition: "" }], rarity: 5 }, "100411": { alternatives: [{ baseDuration: 5e4, condition: "distance_rate>=50&order<=3&blocked_side_continuetime>=2", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 4 }, "100421": { alternatives: [{ baseDuration: 5e4, condition: "remain_distance<=201&remain_distance>=199&distance_diff_top<=5&order>=2", effects: [{ modifier: 4500, target: 1, type: 27 }], precondition: "" }, { baseDuration: 5e4, condition: "remain_distance<=201&remain_distance>=199&distance_diff_top<=10&order>=2", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 5 }, "100441": { alternatives: [{ baseDuration: 6e4, condition: "remain_distance<=300", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "order_rate_out50_continue==1&temptation_count==0&is_finalcorner==1" }], rarity: 5 }, "100451": { alternatives: [{ baseDuration: 0, condition: "phase_random==1&order>=2&order_rate<=40", effects: [{ modifier: 750, target: 1, type: 9 }], precondition: "" }], rarity: 4 }, "100461": { alternatives: [{ baseDuration: 5e4, condition: "phase==1&corner==0&order<=2&bashin_diff_behind<=1", effects: [{ modifier: 2500, target: 1, type: 27 }, { modifier: 3e3, target: 1, type: 31 }], precondition: "" }], rarity: 5 }, "100481": { alternatives: [{ baseDuration: 5e4, condition: "is_finalcorner==1&corner==0&order_rate<=40&overtake_target_time>=1@is_finalcorner==1&corner==0&order_rate<=40&is_overtake==1", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 5 }, "100501": { alternatives: [{ baseDuration: 5e4, condition: "is_finalcorner==1&order_rate>=40&order_rate<=75&is_overtake==1", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 5 }, "100511": { alternatives: [{ baseDuration: 4e4, condition: "phase>=2&is_finalcorner_laterhalf==1&order>=3&order_rate<=40@phase>=2&is_finalcorner==1&corner==0&order>=3&order_rate<=40", effects: [{ modifier: 4e3, target: 1, type: 31 }], precondition: "phase==1&blocked_side_continuetime>=2&corner!=0" }], rarity: 5 }, "100521": { alternatives: [{ baseDuration: 0, condition: "is_finalcorner==1&corner!=0&order_rate>50&near_count>=1", effects: [{ modifier: 550, target: 1, type: 9 }], precondition: "" }], rarity: 4 }, "100531": { alternatives: [{ baseDuration: 2e4, condition: "phase>=2&order_rate>=50&is_finalcorner==1&bashin_diff_infront<=1", effects: [{ modifier: 1e3, target: 1, type: 31 }, { modifier: 1e3, target: 1, type: 31 }], precondition: "" }], rarity: 5 }, "100561": { alternatives: [{ baseDuration: 5e4, condition: "phase>=2&order>=3&blocked_front==1", effects: [{ modifier: 3500, target: 1, type: 27 }, { modifier: 1e3, target: 1, type: 31 }], precondition: "" }], rarity: 4 }, "100581": { alternatives: [{ baseDuration: 5e4, condition: "phase>=2&change_order_onetime<0", effects: [{ modifier: 2500, target: 1, type: 27 }, { modifier: 3e3, target: 1, type: 31 }], precondition: "" }], rarity: 5 }, "100591": { alternatives: [{ baseDuration: 4e4, condition: "phase>=2&corner!=0&is_finalcorner==0&temptation_count==0&order_rate>=50&order_rate<=70@phase==1&corner!=0&is_finalcorner==1&temptation_count==0&order_rate>=50&order_rate<=70", effects: [{ modifier: 4e3, target: 1, type: 31 }], precondition: "" }], rarity: 5 }, "100601": { alternatives: [{ baseDuration: 5e4, condition: "phase>=2&order==3&bashin_diff_behind<=1", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 4 }, "100611": { alternatives: [{ baseDuration: 5e4, condition: "temptation_count==0&remain_distance<=201&remain_distance>=199&order>=4&order_rate<=70", effects: [{ modifier: 4500, target: 1, type: 27 }], precondition: "" }], rarity: 4 }, "100621": { alternatives: [{ baseDuration: 5e4, condition: "distance_rate>=50&phase==1&order>=3&order_rate<=70", effects: [{ modifier: 550, target: 1, type: 9 }, { modifier: 2500, target: 1, type: 27 }], precondition: "" }], rarity: 4 }, "100641": { alternatives: [{ baseDuration: 6e4, condition: "distance_rate>=50&order_rate_in20_continue==1", effects: [{ modifier: 550, target: 1, type: 9 }, { modifier: 2500, target: 1, type: 27 }], precondition: "" }], rarity: 5 }, "100671": { alternatives: [{ baseDuration: 5e4, condition: "is_last_straight_onetime==1&order>=2&order<=5&distance_diff_top<=5", effects: [{ modifier: 4500, target: 1, type: 27 }], precondition: "" }, { baseDuration: 5e4, condition: "is_last_straight_onetime==1&order>=2&order<=5&distance_diff_top>5", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 5 }, "100681": { alternatives: [{ baseDuration: 5e4, condition: "phase==2&straight_front_type==2&order<=2", effects: [{ modifier: 2500, target: 1, type: 27 }, { modifier: 3e3, target: 1, type: 31 }], precondition: "" }, { baseDuration: 5e4, condition: "distance_rate>=50&corner==3&order<=2", effects: [{ modifier: 2500, target: 1, type: 27 }], precondition: "" }], rarity: 5 }, "100691": { alternatives: [{ baseDuration: 5e4, condition: "remain_distance<=300&order_rate<=40&bashin_diff_infront<=1", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 5 }, "10071": { alternatives: [{ baseDuration: 6e4, condition: "distance_rate>=50&distance_rate<=60&order_rate>50", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 3 }, "100711": { alternatives: [{ baseDuration: 4e4, condition: "is_finalcorner==1&corner==0&bashin_diff_behind<=1", effects: [{ modifier: 4500, target: 1, type: 27 }, { modifier: -100, target: 1, type: 9 }], precondition: "" }], rarity: 5 }, "100721": { alternatives: [{ baseDuration: 5e4, condition: "remain_distance<=300&order_rate<=40", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "is_finalcorner==1&change_order_onetime<0" }], rarity: 5 }, "100741": { alternatives: [{ baseDuration: 5e4, condition: "distance_rate>=50&order_rate>=40&order_rate<=80", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 5 }, "10081": { alternatives: [{ baseDuration: 5e4, condition: "order>=3&order_rate<=50&remain_distance<=200&bashin_diff_infront<=1@order>=3&order_rate<=50&remain_distance<=200&bashin_diff_behind<=1", effects: [{ modifier: 2500, target: 1, type: 27 }], precondition: "" }], rarity: 3 }, "10091": { alternatives: [{ baseDuration: 5e4, condition: "distance_rate>=50&order==1&bashin_diff_behind<=1@distance_rate>=50&order==2&is_overtake==1", effects: [{ modifier: 1500, target: 1, type: 27 }, { modifier: 2e3, target: 1, type: 31 }], precondition: "" }], rarity: 3 }, "100981": { alternatives: [{ baseDuration: 5e4, condition: "phase_laterhalf_random==1", effects: [{ modifier: 2500, target: 1, type: 27 }, { modifier: 500, target: 1, type: 27 }, { modifier: 500, target: 1, type: 31 }], precondition: "" }], rarity: 5 }, "100991": { alternatives: [{ baseDuration: 4e4, condition: "is_lastspurt==1&order_rate<=40&order_rate>=30&ground_type==2&lastspurt==2", effects: [{ modifier: 4e3, target: 1, type: 31 }], precondition: "phase==1&blocked_side_continuetime>=2&corner!=0" }, { baseDuration: 4e4, condition: "is_lastspurt==1&order_rate<=40&order_rate>=30&ground_type==2", effects: [{ modifier: 2e3, target: 1, type: 31 }], precondition: "phase==1&blocked_side_continuetime>=2&corner!=0" }], rarity: 5 }, "10111": { alternatives: [{ baseDuration: 5e4, condition: "is_finalcorner==1&corner==0&change_order_onetime<0&order>=3", effects: [{ modifier: 2500, target: 1, type: 27 }], precondition: "" }], rarity: 3 }, "10141": { alternatives: [{ baseDuration: 5e4, condition: "is_finalcorner==1&corner==0&hp_per>=30&order<=2", effects: [{ modifier: 1500, target: 1, type: 27 }, { modifier: 2e3, target: 1, type: 31 }], precondition: "" }], rarity: 3 }, "10181": { alternatives: [{ baseDuration: 5e4, condition: "is_finalcorner==1&corner!=0&order>=3&change_order_onetime<0", effects: [{ modifier: 2500, target: 1, type: 27 }], precondition: "" }], rarity: 3 }, "10241": { alternatives: [{ baseDuration: 5e4, condition: "is_finalcorner==1&blocked_side_continuetime>=2&order<=3", effects: [{ modifier: 1500, target: 1, type: 27 }, { modifier: 2e3, target: 1, type: 31 }], precondition: "" }], rarity: 3 }, "10271": { alternatives: [{ baseDuration: 4e4, condition: "phase>=2&corner!=0&order_rate>=65&order_rate<=70", effects: [{ modifier: 3e3, target: 1, type: 31 }], precondition: "" }], rarity: 3 }, "10321": { alternatives: [{ baseDuration: 4e4, condition: "distance_rate>=50&corner!=0&order>=3&order_rate<=40", effects: [{ modifier: 350, target: 1, type: 9 }, { modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 3 }, "10351": { alternatives: [{ baseDuration: 5e4, condition: "is_finalcorner==1&corner==0&order<=5", effects: [{ modifier: 2500, target: 1, type: 27 }], precondition: "is_finalcorner==1&blocked_side_continuetime>=2" }], rarity: 3 }, "10411": { alternatives: [{ baseDuration: 5e4, condition: "distance_rate>=50&order<=3&blocked_side_continuetime>=2", effects: [{ modifier: 2500, target: 1, type: 27 }], precondition: "" }], rarity: 3 }, "10451": { alternatives: [{ baseDuration: 0, condition: "phase_random==1&order>=2&order_rate<=40", effects: [{ modifier: 550, target: 1, type: 9 }], precondition: "" }], rarity: 3 }, "10521": { alternatives: [{ baseDuration: 0, condition: "is_finalcorner==1&corner!=0&order_rate>50&near_count>=1", effects: [{ modifier: 350, target: 1, type: 9 }], precondition: "" }], rarity: 3 }, "10561": { alternatives: [{ baseDuration: 5e4, condition: "phase>=2&order>=3&blocked_front==1", effects: [{ modifier: 2500, target: 1, type: 27 }, { modifier: 1e3, target: 1, type: 31 }], precondition: "" }], rarity: 3 }, "10601": { alternatives: [{ baseDuration: 5e4, condition: "phase>=2&order==3&bashin_diff_behind<=1", effects: [{ modifier: 2500, target: 1, type: 27 }], precondition: "" }], rarity: 3 }, "10611": { alternatives: [{ baseDuration: 5e4, condition: "temptation_count==0&remain_distance<=201&remain_distance>=199&order>=4&order_rate<=70", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 3 }, "10621": { alternatives: [{ baseDuration: 5e4, condition: "distance_rate>=50&phase==1&order>=3&order_rate<=70", effects: [{ modifier: 350, target: 1, type: 9 }, { modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 3 }, "110011": { alternatives: [{ baseDuration: 0, condition: "phase==1&order>=2&order_rate<=70&activate_count_middle>=2", effects: [{ modifier: 550, target: 1, type: 9 }], precondition: "" }], rarity: 5 }, "110031": { alternatives: [{ baseDuration: 5e4, condition: "is_finalcorner==1&corner==0", effects: [{ modifier: 4500, target: 1, type: 27 }], precondition: "is_finalcorner==1&is_overtake==1&order<=5&order_rate<=50&overtake_target_no_order_up_time>=2" }], rarity: 5 }, "110041": { alternatives: [{ baseDuration: 5e4, condition: "distance_rate>=50&activate_count_heal>=1&order<=3", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 5 }, "110051": { alternatives: [{ baseDuration: 4e4, condition: "is_finalcorner==1&change_order_onetime<0&order_rate<=40", effects: [{ modifier: 4500, target: 1, type: 27 }], precondition: "" }], rarity: 5 }, "110061": { alternatives: [{ baseDuration: 5e4, condition: "activate_count_heal>=3&distance_rate>=50", effects: [{ modifier: 2500, target: 1, type: 27 }, { modifier: 3e3, target: 1, type: 31 }, { modifier: 350, target: 1, type: 9 }], precondition: "" }], rarity: 5 }, "110071": { alternatives: [{ baseDuration: 5e4, condition: "distance_rate_after_random==50", effects: [{ modifier: 1500, target: 1, type: 27 }, { modifier: 2e4, target: 1, type: 37 }], precondition: "" }], rarity: 5 }, "110101": { alternatives: [{ baseDuration: 5e4, condition: "distance_diff_top<=5&order>=2&order_rate<=40&remain_distance<=201&remain_distance>=199", effects: [{ modifier: 3500, target: 1, type: 27 }, { modifier: 1500, target: 1, type: 22 }], precondition: "" }, { baseDuration: 5e4, condition: "order>=2&order_rate<=40&remain_distance<=201&remain_distance>=199", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 5 }, "110111": { alternatives: [{ baseDuration: 0, condition: "phase==1&change_order_onetime>0&order_rate>=40", effects: [{ modifier: 750, target: 1, type: 9 }], precondition: "" }], rarity: 5 }, "110131": { alternatives: [{ baseDuration: 6e4, condition: "phase>=2&is_finalcorner==1&order<=4&overtake_target_time>=1", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 5 }, "110141": { alternatives: [{ baseDuration: 4e4, condition: "is_finalcorner==1&corner!=0&is_overtake==1&order>=4&order_rate<=75", effects: [{ modifier: 4e3, target: 1, type: 31 }], precondition: "" }], rarity: 5 }, "110151": { alternatives: [{ baseDuration: 5e4, condition: "remain_distance<=401&remain_distance>=399&order_rate<=40&activate_count_all>=7", effects: [{ modifier: 4500, target: 1, type: 27 }], precondition: "" }, { baseDuration: 5e4, condition: "remain_distance<=401&remain_distance>=399&order_rate<=40&activate_count_all<=6", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 5 }, "110171": { alternatives: [{ baseDuration: 6e4, condition: "is_finalcorner==1", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "phase>=2&order_rate<=50&overtake_target_time>=2" }], rarity: 5 }, "110181": { alternatives: [{ baseDuration: 5e4, condition: "phase==1&order>=3&order_rate<=50&is_overtake==1", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 5 }, "110201": { alternatives: [{ baseDuration: 5e4, condition: "phase>=2&corner==0&order<=2&straight_front_type==2", effects: [{ modifier: 3500, target: 1, type: 27 }, { modifier: 2e3, target: 1, type: 31 }], precondition: "" }, { baseDuration: 5e4, condition: "phase>=2&corner==0&order<=2", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 5 }, "110221": { alternatives: [{ baseDuration: 5e4, condition: "remain_distance>=401&phase>=2&is_finalcorner==1&order_rate>=20&order_rate<=40", effects: [{ modifier: 3500, target: 1, type: 27 }, { modifier: 1e3, target: 1, type: 31 }], precondition: "" }, { baseDuration: 5e4, condition: "phase>=2&is_finalcorner==1&order_rate>=20&order_rate<=40", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 5 }, "110231": { alternatives: [{ baseDuration: 5e4, condition: "order_rate_in50_continue==1&phase==1&distance_rate>=50&order>=2&order_rate<=40", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 5 }, "110241": { alternatives: [{ baseDuration: 5e4, condition: "is_finalcorner==1&order_rate<=40&change_order_onetime<0", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }, { baseDuration: 4e4, condition: "is_finalcorner==1&order_rate>=50&order_rate<=80&change_order_onetime<0", effects: [{ modifier: 4e3, target: 1, type: 31 }], precondition: "" }], rarity: 5 }, "110261": { alternatives: [{ baseDuration: 5e4, condition: "order<=4&phase==1&corner!=0&bashin_diff_behind<=3", effects: [{ modifier: 3500, target: 1, type: 27 }, { modifier: 150, target: 1, type: 9 }], precondition: "" }], rarity: 5 }, "110301": { alternatives: [{ baseDuration: 5e4, condition: "phase==1&order>=2&order_rate<=50", effects: [{ modifier: 550, target: 1, type: 9 }, { modifier: -50, target: 9, type: 9 }, { modifier: 2500, target: 1, type: 27 }], precondition: "" }], rarity: 5 }, "110371": { alternatives: [{ baseDuration: 6e4, condition: "change_order_up_finalcorner_after>=2&is_finalcorner==1&corner==0", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 5 }, "110381": { alternatives: [{ baseDuration: 5e4, condition: "remain_distance<=350&order_rate<=40&order_rate>=20&bashin_diff_behind<=1", effects: [{ modifier: 2500, target: 1, type: 27 }, { modifier: 3e3, target: 1, type: 31 }], precondition: "" }], rarity: 5 }, "110401": { alternatives: [{ baseDuration: 5e4, condition: "is_finalcorner==1&corner!=0&order_rate>=30&order_rate<=70&blocked_side_continuetime>=2", effects: [{ modifier: 2500, target: 1, type: 27 }, { modifier: 3e3, target: 1, type: 31 }], precondition: "" }], rarity: 5 }, "110451": { alternatives: [{ baseDuration: 5e4, condition: "is_finalcorner==1&corner==0&order<=4&bashin_diff_infront<=1", effects: [{ modifier: 2500, target: 1, type: 27 }, { modifier: 3e3, target: 1, type: 31 }], precondition: "" }], rarity: 5 }, "110521": { alternatives: [{ baseDuration: 5e4, condition: "is_finalcorner==1&corner!=0&distance_diff_top>=7", effects: [{ modifier: 2500, target: 1, type: 27 }], precondition: "" }], rarity: 5 }, "110561": { alternatives: [{ baseDuration: 5e4, condition: "distance_rate>=50&order_rate>=40&overtake_target_time>=2", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 5 }, "110591": { alternatives: [{ baseDuration: 5e4, condition: "distance_rate>=60&slope==2&phase==1&order_rate>=40&order_rate<=80&remain_distance>=500", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 5 }, "110601": { alternatives: [{ baseDuration: 6e4, condition: "is_finalcorner==1&corner==0&order_rate>=40&order_rate<=70&popularity>=4", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "is_finalcorner==1&change_order_onetime<0" }, { baseDuration: 5e4, condition: "is_finalcorner==1&corner==0&order_rate>=40&order_rate<=70&popularity<4", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "is_finalcorner==1&change_order_onetime<0" }], rarity: 5 }, "110611": { alternatives: [{ baseDuration: 4e4, condition: "is_finalcorner==1&corner==0", effects: [{ modifier: 4e3, target: 1, type: 31 }], precondition: "distance_rate>=50&order_rate_out70_continue==1&temptation_count==0" }], rarity: 5 }, "120011": { alternatives: [{ baseDuration: 5e4, condition: "phase>=2&is_finalcorner==1&corner!=0&is_activate_any_skill==1", effects: [{ modifier: 4500, target: 1, type: 27 }], precondition: "track_id==10005" }, { baseDuration: 5e4, condition: "phase>=2&is_finalcorner==1&corner!=0&is_activate_any_skill==1", effects: [{ modifier: 2500, target: 1, type: 27 }], precondition: "" }], rarity: 5 }, "120131": { alternatives: [{ baseDuration: 5e4, condition: "distance_rate>=50&order_rate<=40", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 5 }, "200011": { alternatives: [{ baseDuration: -1, condition: "rotation==1", effects: [{ modifier: 6e5, target: 1, type: 1 }], precondition: "" }], rarity: 1 }, "200012": { alternatives: [{ baseDuration: -1, condition: "rotation==1", effects: [{ modifier: 4e5, target: 1, type: 1 }], precondition: "" }], rarity: 1 }, "200013": { alternatives: [{ baseDuration: -1, condition: "rotation==1", effects: [{ modifier: -4e5, target: 1, type: 1 }], precondition: "" }], rarity: 1 }, "200014": { alternatives: [{ baseDuration: -1, condition: "rotation==1", effects: [{ modifier: 6e5, target: 1, type: 1 }, { modifier: 6e5, target: 1, type: 3 }], precondition: "" }], rarity: 2 }, "200021": { alternatives: [{ baseDuration: -1, condition: "rotation==2", effects: [{ modifier: 6e5, target: 1, type: 1 }], precondition: "" }], rarity: 1 }, "200022": { alternatives: [{ baseDuration: -1, condition: "rotation==2", effects: [{ modifier: 4e5, target: 1, type: 1 }], precondition: "" }], rarity: 1 }, "200023": { alternatives: [{ baseDuration: -1, condition: "rotation==2", effects: [{ modifier: -4e5, target: 1, type: 1 }], precondition: "" }], rarity: 1 }, "200031": { alternatives: [{ baseDuration: -1, condition: "track_id==10006", effects: [{ modifier: 6e5, target: 1, type: 2 }], precondition: "" }], rarity: 1 }, "200032": { alternatives: [{ baseDuration: -1, condition: "track_id==10006", effects: [{ modifier: 4e5, target: 1, type: 2 }], precondition: "" }], rarity: 1 }, "200033": { alternatives: [{ baseDuration: -1, condition: "track_id==10006", effects: [{ modifier: -4e5, target: 1, type: 2 }], precondition: "" }], rarity: 1 }, "200041": { alternatives: [{ baseDuration: -1, condition: "track_id==10005", effects: [{ modifier: 6e5, target: 1, type: 2 }], precondition: "" }], rarity: 1 }, "200042": { alternatives: [{ baseDuration: -1, condition: "track_id==10005", effects: [{ modifier: 4e5, target: 1, type: 2 }], precondition: "" }], rarity: 1 }, "200043": { alternatives: [{ baseDuration: -1, condition: "track_id==10005", effects: [{ modifier: -4e5, target: 1, type: 2 }], precondition: "" }], rarity: 1 }, "200051": { alternatives: [{ baseDuration: -1, condition: "track_id==10009", effects: [{ modifier: 6e5, target: 1, type: 2 }], precondition: "" }], rarity: 1 }, "200052": { alternatives: [{ baseDuration: -1, condition: "track_id==10009", effects: [{ modifier: 4e5, target: 1, type: 2 }], precondition: "" }], rarity: 1 }, "200053": { alternatives: [{ baseDuration: -1, condition: "track_id==10009", effects: [{ modifier: -4e5, target: 1, type: 2 }], precondition: "" }], rarity: 1 }, "200061": { alternatives: [{ baseDuration: -1, condition: "track_id==10008", effects: [{ modifier: 6e5, target: 1, type: 2 }], precondition: "" }], rarity: 1 }, "200062": { alternatives: [{ baseDuration: -1, condition: "track_id==10008", effects: [{ modifier: 4e5, target: 1, type: 2 }], precondition: "" }], rarity: 1 }, "200063": { alternatives: [{ baseDuration: -1, condition: "track_id==10008", effects: [{ modifier: -4e5, target: 1, type: 2 }], precondition: "" }], rarity: 1 }, "200064": { alternatives: [{ baseDuration: -1, condition: "track_id==10008", effects: [{ modifier: 6e5, target: 1, type: 2 }, { modifier: 6e5, target: 1, type: 5 }, { modifier: 6e5, target: 1, type: 1 }], precondition: "" }], rarity: 2 }, "200071": { alternatives: [{ baseDuration: -1, condition: "track_id==10007", effects: [{ modifier: 6e5, target: 1, type: 2 }], precondition: "" }], rarity: 1 }, "200072": { alternatives: [{ baseDuration: -1, condition: "track_id==10007", effects: [{ modifier: 4e5, target: 1, type: 2 }], precondition: "" }], rarity: 1 }, "200073": { alternatives: [{ baseDuration: -1, condition: "track_id==10007", effects: [{ modifier: -4e5, target: 1, type: 2 }], precondition: "" }], rarity: 1 }, "200081": { alternatives: [{ baseDuration: -1, condition: "track_id==10001", effects: [{ modifier: 6e5, target: 1, type: 2 }], precondition: "" }], rarity: 1 }, "200082": { alternatives: [{ baseDuration: -1, condition: "track_id==10001", effects: [{ modifier: 4e5, target: 1, type: 2 }], precondition: "" }], rarity: 1 }, "200083": { alternatives: [{ baseDuration: -1, condition: "track_id==10001", effects: [{ modifier: -4e5, target: 1, type: 2 }], precondition: "" }], rarity: 1 }, "200091": { alternatives: [{ baseDuration: -1, condition: "track_id==10002", effects: [{ modifier: 6e5, target: 1, type: 2 }], precondition: "" }], rarity: 1 }, "200092": { alternatives: [{ baseDuration: -1, condition: "track_id==10002", effects: [{ modifier: 4e5, target: 1, type: 2 }], precondition: "" }], rarity: 1 }, "200093": { alternatives: [{ baseDuration: -1, condition: "track_id==10002", effects: [{ modifier: -4e5, target: 1, type: 2 }], precondition: "" }], rarity: 1 }, "200101": { alternatives: [{ baseDuration: -1, condition: "track_id==10004", effects: [{ modifier: 6e5, target: 1, type: 2 }], precondition: "" }], rarity: 1 }, "200102": { alternatives: [{ baseDuration: -1, condition: "track_id==10004", effects: [{ modifier: 4e5, target: 1, type: 2 }], precondition: "" }], rarity: 1 }, "200103": { alternatives: [{ baseDuration: -1, condition: "track_id==10004", effects: [{ modifier: -4e5, target: 1, type: 2 }], precondition: "" }], rarity: 1 }, "200111": { alternatives: [{ baseDuration: -1, condition: "track_id==10003", effects: [{ modifier: 6e5, target: 1, type: 2 }], precondition: "" }], rarity: 1 }, "200112": { alternatives: [{ baseDuration: -1, condition: "track_id==10003", effects: [{ modifier: 4e5, target: 1, type: 2 }], precondition: "" }], rarity: 1 }, "200113": { alternatives: [{ baseDuration: -1, condition: "track_id==10003", effects: [{ modifier: -4e5, target: 1, type: 2 }], precondition: "" }], rarity: 1 }, "200121": { alternatives: [{ baseDuration: -1, condition: "track_id==10010", effects: [{ modifier: 6e5, target: 1, type: 2 }], precondition: "" }], rarity: 1 }, "200122": { alternatives: [{ baseDuration: -1, condition: "track_id==10010", effects: [{ modifier: 4e5, target: 1, type: 2 }], precondition: "" }], rarity: 1 }, "200123": { alternatives: [{ baseDuration: -1, condition: "track_id==10010", effects: [{ modifier: -4e5, target: 1, type: 2 }], precondition: "" }], rarity: 1 }, "200131": { alternatives: [{ baseDuration: -1, condition: "is_basis_distance==1", effects: [{ modifier: 6e5, target: 1, type: 2 }], precondition: "" }], rarity: 1 }, "200132": { alternatives: [{ baseDuration: -1, condition: "is_basis_distance==1", effects: [{ modifier: 4e5, target: 1, type: 2 }], precondition: "" }], rarity: 1 }, "200133": { alternatives: [{ baseDuration: -1, condition: "is_basis_distance==1", effects: [{ modifier: -4e5, target: 1, type: 2 }], precondition: "" }], rarity: 1 }, "200141": { alternatives: [{ baseDuration: -1, condition: "is_basis_distance==0", effects: [{ modifier: 6e5, target: 1, type: 2 }], precondition: "" }], rarity: 1 }, "200142": { alternatives: [{ baseDuration: -1, condition: "is_basis_distance==0", effects: [{ modifier: 4e5, target: 1, type: 2 }], precondition: "" }], rarity: 1 }, "200143": { alternatives: [{ baseDuration: -1, condition: "is_basis_distance==0", effects: [{ modifier: -4e5, target: 1, type: 2 }], precondition: "" }], rarity: 1 }, "200151": { alternatives: [{ baseDuration: -1, condition: "ground_condition==1", effects: [{ modifier: 6e5, target: 1, type: 3 }], precondition: "" }], rarity: 1 }, "200152": { alternatives: [{ baseDuration: -1, condition: "ground_condition==1", effects: [{ modifier: 4e5, target: 1, type: 3 }], precondition: "" }], rarity: 1 }, "200153": { alternatives: [{ baseDuration: -1, condition: "ground_condition==1", effects: [{ modifier: -4e5, target: 1, type: 3 }], precondition: "" }], rarity: 1 }, "200154": { alternatives: [{ baseDuration: -1, condition: "ground_condition==1", effects: [{ modifier: 6e5, target: 1, type: 3 }, { modifier: 6e5, target: 1, type: 1 }], precondition: "" }], rarity: 2 }, "200161": { alternatives: [{ baseDuration: -1, condition: "ground_condition==2@ground_condition==3@ground_condition==4", effects: [{ modifier: 6e5, target: 1, type: 3 }], precondition: "" }], rarity: 1 }, "200162": { alternatives: [{ baseDuration: -1, condition: "ground_condition==2@ground_condition==3@ground_condition==4", effects: [{ modifier: 4e5, target: 1, type: 3 }], precondition: "" }], rarity: 1 }, "200163": { alternatives: [{ baseDuration: -1, condition: "ground_condition==2@ground_condition==3@ground_condition==4", effects: [{ modifier: -4e5, target: 1, type: 3 }], precondition: "" }], rarity: 1 }, "200171": { alternatives: [{ baseDuration: -1, condition: "season==1@season==5", effects: [{ modifier: 6e5, target: 1, type: 1 }], precondition: "" }], rarity: 1 }, "200172": { alternatives: [{ baseDuration: -1, condition: "season==1@season==5", effects: [{ modifier: 4e5, target: 1, type: 1 }], precondition: "" }], rarity: 1 }, "200173": { alternatives: [{ baseDuration: -1, condition: "season==1@season==5", effects: [{ modifier: -4e5, target: 1, type: 1 }], precondition: "" }], rarity: 1 }, "200174": { alternatives: [{ baseDuration: -1, condition: "season==1@season==5", effects: [{ modifier: 6e5, target: 1, type: 1 }, { modifier: 6e5, target: 1, type: 3 }], precondition: "" }], rarity: 2 }, "200181": { alternatives: [{ baseDuration: -1, condition: "season==2", effects: [{ modifier: 6e5, target: 1, type: 1 }], precondition: "" }], rarity: 1 }, "200182": { alternatives: [{ baseDuration: -1, condition: "season==2", effects: [{ modifier: 4e5, target: 1, type: 1 }], precondition: "" }], rarity: 1 }, "200183": { alternatives: [{ baseDuration: -1, condition: "season==2", effects: [{ modifier: -4e5, target: 1, type: 1 }], precondition: "" }], rarity: 1 }, "200191": { alternatives: [{ baseDuration: -1, condition: "season==3", effects: [{ modifier: 6e5, target: 1, type: 1 }], precondition: "" }], rarity: 1 }, "200192": { alternatives: [{ baseDuration: -1, condition: "season==3", effects: [{ modifier: 4e5, target: 1, type: 1 }], precondition: "" }], rarity: 1 }, "200193": { alternatives: [{ baseDuration: -1, condition: "season==3", effects: [{ modifier: -4e5, target: 1, type: 1 }], precondition: "" }], rarity: 1 }, "200194": { alternatives: [{ baseDuration: -1, condition: "season==3", effects: [{ modifier: 6e5, target: 1, type: 1 }, { modifier: 6e5, target: 1, type: 3 }], precondition: "" }], rarity: 2 }, "200201": { alternatives: [{ baseDuration: -1, condition: "season==4", effects: [{ modifier: 6e5, target: 1, type: 1 }], precondition: "" }], rarity: 1 }, "200202": { alternatives: [{ baseDuration: -1, condition: "season==4", effects: [{ modifier: 4e5, target: 1, type: 1 }], precondition: "" }], rarity: 1 }, "200203": { alternatives: [{ baseDuration: -1, condition: "season==4", effects: [{ modifier: -4e5, target: 1, type: 1 }], precondition: "" }], rarity: 1 }, "200211": { alternatives: [{ baseDuration: -1, condition: "weather==1", effects: [{ modifier: 6e5, target: 1, type: 4 }], precondition: "" }], rarity: 1 }, "200212": { alternatives: [{ baseDuration: -1, condition: "weather==1", effects: [{ modifier: 4e5, target: 1, type: 4 }], precondition: "" }], rarity: 1 }, "200221": { alternatives: [{ baseDuration: -1, condition: "weather==2", effects: [{ modifier: 6e5, target: 1, type: 4 }], precondition: "" }], rarity: 1 }, "200222": { alternatives: [{ baseDuration: -1, condition: "weather==2", effects: [{ modifier: 4e5, target: 1, type: 4 }], precondition: "" }], rarity: 1 }, "200231": { alternatives: [{ baseDuration: -1, condition: "weather==3", effects: [{ modifier: 6e5, target: 1, type: 4 }], precondition: "" }], rarity: 1 }, "200232": { alternatives: [{ baseDuration: -1, condition: "weather==3", effects: [{ modifier: 4e5, target: 1, type: 4 }], precondition: "" }], rarity: 1 }, "200233": { alternatives: [{ baseDuration: -1, condition: "weather==3", effects: [{ modifier: -4e5, target: 1, type: 4 }], precondition: "" }], rarity: 1 }, "200241": { alternatives: [{ baseDuration: -1, condition: "weather==4", effects: [{ modifier: 6e5, target: 1, type: 4 }], precondition: "" }], rarity: 1 }, "200242": { alternatives: [{ baseDuration: -1, condition: "weather==4", effects: [{ modifier: 4e5, target: 1, type: 4 }], precondition: "" }], rarity: 1 }, "200251": { alternatives: [{ baseDuration: -1, condition: "post_number<=3", effects: [{ modifier: 6e5, target: 1, type: 5 }], precondition: "" }], rarity: 1 }, "200252": { alternatives: [{ baseDuration: -1, condition: "post_number<=3", effects: [{ modifier: 4e5, target: 1, type: 5 }], precondition: "" }], rarity: 1 }, "200253": { alternatives: [{ baseDuration: -1, condition: "post_number<=3", effects: [{ modifier: -4e5, target: 1, type: 5 }], precondition: "" }], rarity: 1 }, "200261": { alternatives: [{ baseDuration: -1, condition: "post_number>=6", effects: [{ modifier: 6e5, target: 1, type: 1 }], precondition: "" }], rarity: 1 }, "200262": { alternatives: [{ baseDuration: -1, condition: "post_number>=6", effects: [{ modifier: 4e5, target: 1, type: 1 }], precondition: "" }], rarity: 1 }, "200263": { alternatives: [{ baseDuration: -1, condition: "post_number>=6", effects: [{ modifier: -4e5, target: 1, type: 1 }], precondition: "" }], rarity: 1 }, "200271": { alternatives: [{ baseDuration: -1, condition: "running_style_count_same<=1", effects: [{ modifier: 8e5, target: 1, type: 1 }], precondition: "" }], rarity: 1 }, "200272": { alternatives: [{ baseDuration: -1, condition: "running_style_count_same<=1", effects: [{ modifier: 6e5, target: 1, type: 1 }], precondition: "" }], rarity: 1 }, "200281": { alternatives: [{ baseDuration: -1, condition: "running_style_count_same_rate>=40", effects: [{ modifier: 6e5, target: 1, type: 3 }], precondition: "" }], rarity: 1 }, "200282": { alternatives: [{ baseDuration: -1, condition: "running_style_count_same_rate>=40", effects: [{ modifier: 4e5, target: 1, type: 3 }], precondition: "" }], rarity: 1 }, "200283": { alternatives: [{ baseDuration: -1, condition: "running_style_count_same_rate>=40", effects: [{ modifier: -4e5, target: 1, type: 3 }], precondition: "" }], rarity: 1 }, "200291": { alternatives: [{ baseDuration: -1, condition: "running_style_equal_popularity_one==1", effects: [{ modifier: 6e5, target: 1, type: 4 }], precondition: "" }], rarity: 1 }, "200292": { alternatives: [{ baseDuration: -1, condition: "running_style_equal_popularity_one==1", effects: [{ modifier: 4e5, target: 1, type: 4 }], precondition: "" }], rarity: 1 }, "200301": { alternatives: [{ baseDuration: -1, condition: "popularity>=4", effects: [{ modifier: 6e5, target: 1, type: 1 }], precondition: "" }], rarity: 1 }, "200302": { alternatives: [{ baseDuration: -1, condition: "popularity>=4", effects: [{ modifier: 4e5, target: 1, type: 1 }], precondition: "" }], rarity: 1 }, "200311": { alternatives: [{ baseDuration: -1, condition: "grade==100", effects: [{ modifier: -4e5, target: 1, type: 1 }], precondition: "" }], rarity: 1 }, "200321": { alternatives: [{ baseDuration: -1, condition: "popularity==1", effects: [{ modifier: -4e5, target: 1, type: 2 }], precondition: "" }], rarity: 1 }, "200331": { alternatives: [{ baseDuration: 24e3, condition: "all_corner_random==1", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 2 }, "200332": { alternatives: [{ baseDuration: 24e3, condition: "all_corner_random==1", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "200333": { alternatives: [{ baseDuration: 24e3, condition: "all_corner_random==1", effects: [{ modifier: -2e3, target: 1, type: 21 }], precondition: "" }], rarity: 1 }, "200341": { alternatives: [{ baseDuration: 3e4, condition: "all_corner_random==1", effects: [{ modifier: 4e3, target: 1, type: 31 }], precondition: "" }], rarity: 2 }, "200342": { alternatives: [{ baseDuration: 3e4, condition: "all_corner_random==1", effects: [{ modifier: 2e3, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "200343": { alternatives: [{ baseDuration: 3e4, condition: "all_corner_random==1", effects: [{ modifier: -2e3, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "200351": { alternatives: [{ baseDuration: 0, condition: "corner_random==1@corner_random==2@corner_random==3@corner_random==4", effects: [{ modifier: 550, target: 1, type: 9 }], precondition: "" }], rarity: 2 }, "200352": { alternatives: [{ baseDuration: 0, condition: "corner_random==1@corner_random==2@corner_random==3@corner_random==4", effects: [{ modifier: 150, target: 1, type: 9 }], precondition: "" }], rarity: 1 }, "200353": { alternatives: [{ baseDuration: 0, condition: "corner_random==1@corner_random==2@corner_random==3@corner_random==4", effects: [{ modifier: -200, target: 1, type: 9 }], precondition: "" }], rarity: 1 }, "200361": { alternatives: [{ baseDuration: 3e4, condition: "straight_random==1", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 2 }, "200362": { alternatives: [{ baseDuration: 3e4, condition: "straight_random==1", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "200371": { alternatives: [{ baseDuration: 3e4, condition: "straight_random==1", effects: [{ modifier: 4e3, target: 1, type: 31 }, { modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 2 }, "200372": { alternatives: [{ baseDuration: 3e4, condition: "straight_random==1", effects: [{ modifier: 2e3, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "200381": { alternatives: [{ baseDuration: 0, condition: "phase==1&corner==0", effects: [{ modifier: 550, target: 1, type: 9 }], precondition: "" }], rarity: 2 }, "200382": { alternatives: [{ baseDuration: 0, condition: "phase==1&corner==0", effects: [{ modifier: 150, target: 1, type: 9 }], precondition: "" }], rarity: 1 }, "200391": { alternatives: [{ baseDuration: 0, condition: "up_slope_random==1", effects: [{ modifier: -200, target: 1, type: 9 }], precondition: "" }], rarity: 1 }, "200401": { alternatives: [{ baseDuration: 0, condition: "accumulatetime>=2&is_surrounded==1", effects: [{ modifier: -200, target: 1, type: 9 }], precondition: "" }], rarity: 1 }, "200411": { alternatives: [{ baseDuration: 3e4, condition: "last_straight_random==1&distance_diff_rate>=75", effects: [{ modifier: -2e3, target: 1, type: 21 }], precondition: "" }], rarity: 1 }, "200421": { alternatives: [{ baseDuration: 3e4, condition: "remain_distance==200&order==1&bashin_diff_behind>=1", effects: [{ modifier: -2e3, target: 1, type: 21 }], precondition: "" }], rarity: 1 }, "200431": { alternatives: [{ baseDuration: 0, condition: "always==1", effects: [{ modifier: 4e3, target: 1, type: 10 }], precondition: "" }], rarity: 2 }, "200432": { alternatives: [{ baseDuration: 0, condition: "always==1", effects: [{ modifier: 9e3, target: 1, type: 10 }], precondition: "" }], rarity: 1 }, "200433": { alternatives: [{ baseDuration: 0, condition: "always==1", effects: [{ modifier: 15e3, target: 1, type: 10 }], precondition: "" }], rarity: 1 }, "200441": { alternatives: [{ baseDuration: 2e4, condition: "phase<=1&accumulatetime>=5&blocked_front_continuetime>=1", effects: [{ modifier: 550, target: 1, type: 9 }, { modifier: 350, target: 1, type: 28 }], precondition: "" }], rarity: 2 }, "200442": { alternatives: [{ baseDuration: 2e4, condition: "phase<=1&accumulatetime>=5&blocked_front_continuetime>=1", effects: [{ modifier: 150, target: 1, type: 9 }, { modifier: 150, target: 1, type: 28 }], precondition: "" }], rarity: 1 }, "200451": { alternatives: [{ baseDuration: 3e4, condition: "phase_random==0", effects: [{ modifier: 450, target: 1, type: 28 }], precondition: "" }], rarity: 2 }, "200452": { alternatives: [{ baseDuration: 3e4, condition: "phase_random==0", effects: [{ modifier: 350, target: 1, type: 28 }], precondition: "" }], rarity: 1 }, "200461": { alternatives: [{ baseDuration: 18e3, condition: "phase==1&change_order_onetime<0", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 2 }, "200462": { alternatives: [{ baseDuration: 18e3, condition: "phase==1&change_order_onetime<0", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "200471": { alternatives: [{ baseDuration: 0, condition: "phase==1&change_order_onetime>0", effects: [{ modifier: 550, target: 1, type: 9 }], precondition: "" }], rarity: 2 }, "200472": { alternatives: [{ baseDuration: 0, condition: "phase==1&change_order_onetime>0", effects: [{ modifier: 150, target: 1, type: 9 }], precondition: "" }], rarity: 1 }, "200481": { alternatives: [{ baseDuration: 0, condition: "phase==1&is_surrounded==1", effects: [{ modifier: 550, target: 1, type: 9 }], precondition: "" }], rarity: 2 }, "200482": { alternatives: [{ baseDuration: 0, condition: "phase==1&is_surrounded==1", effects: [{ modifier: 150, target: 1, type: 9 }], precondition: "" }], rarity: 1 }, "200491": { alternatives: [{ baseDuration: 3e4, condition: "infront_near_lane_time>=1&is_lastspurt==1&hp_per>=1", effects: [{ modifier: 4e3, target: 1, type: 31 }, { modifier: 250, target: 1, type: 28 }], precondition: "" }], rarity: 2 }, "200492": { alternatives: [{ baseDuration: 3e4, condition: "infront_near_lane_time>=1&is_lastspurt==1&hp_per>=1", effects: [{ modifier: 2e3, target: 1, type: 31 }, { modifier: 50, target: 1, type: 28 }], precondition: "" }], rarity: 1 }, "200501": { alternatives: [{ baseDuration: 3e4, condition: "phase_random==2", effects: [{ modifier: 350, target: 1, type: 28 }], precondition: "" }], rarity: 2 }, "200502": { alternatives: [{ baseDuration: 3e4, condition: "phase_random==2", effects: [{ modifier: 250, target: 1, type: 28 }], precondition: "" }], rarity: 1 }, "200511": { alternatives: [{ baseDuration: 24e3, condition: "is_lastspurt==1&phase_firsthalf_random==3", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 2 }, "200512": { alternatives: [{ baseDuration: 24e3, condition: "is_lastspurt==1&phase_firsthalf_random==3", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "200521": { alternatives: [{ baseDuration: 0, condition: "accumulatetime>=2&order==1&bashin_diff_behind>=1", effects: [{ modifier: -200, target: 1, type: 9 }], precondition: "" }], rarity: 1 }, "200531": { alternatives: [{ baseDuration: 12e3, condition: "running_style==1&phase==0", effects: [{ modifier: 4e3, target: 1, type: 31 }], precondition: "" }], rarity: 2 }, "200532": { alternatives: [{ baseDuration: 12e3, condition: "running_style==1&phase==0", effects: [{ modifier: 2e3, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "200541": { alternatives: [{ baseDuration: 3e4, condition: "running_style==1&phase_random==1&order_rate<=50", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 2 }, "200542": { alternatives: [{ baseDuration: 3e4, condition: "running_style==1&phase_random==1&order_rate<=50", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "200551": { alternatives: [{ baseDuration: 3e4, condition: "running_style==1&is_finalcorner_random==1&order==1", effects: [{ modifier: 4e3, target: 1, type: 31 }], precondition: "" }], rarity: 2 }, "200552": { alternatives: [{ baseDuration: 3e4, condition: "running_style==1&is_finalcorner_random==1&order==1", effects: [{ modifier: 2e3, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "200561": { alternatives: [{ baseDuration: 0, condition: "running_style==2&phase_laterhalf_random==0&order_rate<=50", effects: [{ modifier: 550, target: 1, type: 9 }], precondition: "" }], rarity: 2 }, "200562": { alternatives: [{ baseDuration: 0, condition: "running_style==2&phase_laterhalf_random==0&order_rate<=50", effects: [{ modifier: 150, target: 1, type: 9 }], precondition: "" }], rarity: 1 }, "200571": { alternatives: [{ baseDuration: 0, condition: "running_style==2&phase_random==1&order_rate<=50", effects: [{ modifier: 550, target: 1, type: 9 }], precondition: "" }], rarity: 2 }, "200572": { alternatives: [{ baseDuration: 0, condition: "running_style==2&phase_random==1&order_rate<=50", effects: [{ modifier: 150, target: 1, type: 9 }], precondition: "" }], rarity: 1 }, "200581": { alternatives: [{ baseDuration: 18e3, condition: "running_style==2&is_finalcorner_random==1&order_rate<=50", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 2 }, "200582": { alternatives: [{ baseDuration: 18e3, condition: "running_style==2&is_finalcorner_random==1&order_rate<=50", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "200591": { alternatives: [{ baseDuration: 24e3, condition: "running_style==3&phase_random==1&order_rate>=40", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 2 }, "200592": { alternatives: [{ baseDuration: 24e3, condition: "running_style==3&phase_random==1&order_rate>=40", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "200601": { alternatives: [{ baseDuration: 18e3, condition: "running_style==3&phase_firsthalf_random==2&order_rate>=40", effects: [{ modifier: 4e3, target: 1, type: 31 }], precondition: "" }], rarity: 2 }, "200602": { alternatives: [{ baseDuration: 18e3, condition: "running_style==3&phase_firsthalf_random==2&order_rate>=40", effects: [{ modifier: 2e3, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "200611": { alternatives: [{ baseDuration: 3e4, condition: "running_style==3&is_finalcorner==1&corner!=0&is_behind_in==1&change_order_onetime<0", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 2 }, "200612": { alternatives: [{ baseDuration: 3e4, condition: "running_style==3&is_finalcorner==1&corner!=0&is_behind_in==1&change_order_onetime<0", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "200621": { alternatives: [{ baseDuration: 0, condition: "running_style==4&phase_random==1&distance_diff_rate>=75", effects: [{ modifier: 550, target: 1, type: 9 }], precondition: "" }], rarity: 2 }, "200622": { alternatives: [{ baseDuration: 0, condition: "running_style==4&phase_random==1&distance_diff_rate>=75", effects: [{ modifier: 150, target: 1, type: 9 }], precondition: "" }], rarity: 1 }, "200631": { alternatives: [{ baseDuration: 3e4, condition: "running_style==4&phase_firsthalf_random==3&is_lastspurt==1", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "phase==2&distance_diff_rate>=50" }], rarity: 2 }, "200632": { alternatives: [{ baseDuration: 3e4, condition: "running_style==4&phase_firsthalf_random==3&is_lastspurt==1", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "phase==2&distance_diff_rate>=50" }], rarity: 1 }, "200641": { alternatives: [{ baseDuration: 9e3, condition: "running_style==4&is_lastspurt==1&corner==0", effects: [{ modifier: 4e3, target: 1, type: 31 }], precondition: "" }], rarity: 2 }, "200642": { alternatives: [{ baseDuration: 9e3, condition: "running_style==4&is_lastspurt==1&corner==0", effects: [{ modifier: 2e3, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "200651": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==1&phase_random==2", effects: [{ modifier: 4e3, target: 1, type: 31 }], precondition: "" }], rarity: 2 }, "200652": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==1&phase_random==2", effects: [{ modifier: 2e3, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "200662": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==1&phase_random==1&order_rate>50", effects: [{ modifier: 150, target: 1, type: 9 }, { modifier: 1e3, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "200671": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==1&phase_random==2&order_rate>50", effects: [{ modifier: 3500, target: 1, type: 27 }, { modifier: 1e3, target: 1, type: 31 }], precondition: "" }], rarity: 2 }, "200672": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==1&phase_random==2&order_rate>50", effects: [{ modifier: 1500, target: 1, type: 27 }, { modifier: 500, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "200681": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==2&phase_laterhalf_random==0&order_rate<=50", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 2 }, "200682": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==2&phase_laterhalf_random==0&order_rate<=50", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "200691": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==2&phase_laterhalf_random==0&order_rate>50", effects: [{ modifier: 550, target: 1, type: 9 }, { modifier: -2e3, target: 9, type: 21 }], precondition: "" }], rarity: 2 }, "200692": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==2&phase_laterhalf_random==0&order_rate>50", effects: [{ modifier: 150, target: 1, type: 9 }, { modifier: -500, target: 9, type: 21 }], precondition: "" }], rarity: 1 }, "200701": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==2&phase_random==2&order_rate>50", effects: [{ modifier: 4e3, target: 1, type: 31 }], precondition: "" }], rarity: 2 }, "200702": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==2&phase_random==2&order_rate>50", effects: [{ modifier: 2e3, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "200711": { alternatives: [{ baseDuration: 0, condition: "distance_type==3&phase_random==1&order<=3", effects: [{ modifier: 550, target: 1, type: 9 }], precondition: "" }], rarity: 2 }, "200712": { alternatives: [{ baseDuration: 0, condition: "distance_type==3&phase_random==1&order<=3", effects: [{ modifier: 150, target: 1, type: 9 }], precondition: "" }], rarity: 1 }, "200721": { alternatives: [{ baseDuration: 24e3, condition: "distance_type==3&phase_random==1&order_rate<=50", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 2 }, "200722": { alternatives: [{ baseDuration: 24e3, condition: "distance_type==3&phase_random==1&order_rate<=50", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "200731": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==3&is_finalcorner==1&overtake_target_time>=2", effects: [{ modifier: 3500, target: 1, type: 27 }, { modifier: 1e3, target: 1, type: 31 }], precondition: "" }], rarity: 2 }, "200732": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==3&is_finalcorner==1&overtake_target_time>=2", effects: [{ modifier: 1500, target: 1, type: 27 }, { modifier: 500, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "200741": { alternatives: [{ baseDuration: 0, condition: "distance_type==4&phase_random==1", effects: [{ modifier: 550, target: 1, type: 9 }], precondition: "" }], rarity: 2 }, "200742": { alternatives: [{ baseDuration: 0, condition: "distance_type==4&phase_random==1", effects: [{ modifier: 150, target: 1, type: 9 }], precondition: "" }], rarity: 1 }, "200751": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==4&is_finalcorner==1&corner!=0&lane_type==0", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 2 }, "200752": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==4&is_finalcorner==1&corner!=0&lane_type==0", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "200761": { alternatives: [{ baseDuration: 18e3, condition: "distance_type==4&hp_per<=30", effects: [{ modifier: 550, target: 1, type: 9 }, { modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 2 }, "200762": { alternatives: [{ baseDuration: 0, condition: "distance_type==4&hp_per<=30", effects: [{ modifier: 150, target: 1, type: 9 }], precondition: "" }], rarity: 1 }, "200771": { alternatives: [{ baseDuration: 0, condition: "phase==1&order_rate<=50&temptation_opponent_count_behind>=1", effects: [{ modifier: -100, target: 20, type: 9 }], precondition: "" }], rarity: 1 }, "200772": { alternatives: [{ baseDuration: 0, condition: "phase==1&order_rate<=50&temptation_opponent_count_behind>=1", effects: [{ modifier: -300, target: 20, type: 9 }], precondition: "" }], rarity: 2 }, "200781": { alternatives: [{ baseDuration: 0, condition: "phase==1&order_rate>50&temptation_opponent_count_infront>=1", effects: [{ modifier: -100, target: 19, type: 9 }], precondition: "" }], rarity: 1 }, "200791": { alternatives: [{ baseDuration: 0, condition: "running_style_temptation_opponent_count_nige>=1&is_temptation==0", effects: [{ modifier: 5e4, target: 21, type: 13 }], precondition: "" }], rarity: 1 }, "200801": { alternatives: [{ baseDuration: 0, condition: "running_style_temptation_opponent_count_senko>=1&is_temptation==0", effects: [{ modifier: 5e4, target: 21, type: 13 }], precondition: "" }], rarity: 1 }, "200811": { alternatives: [{ baseDuration: 0, condition: "running_style_temptation_opponent_count_sashi>=1&is_temptation==0", effects: [{ modifier: 5e4, target: 21, type: 13 }], precondition: "" }], rarity: 1 }, "200821": { alternatives: [{ baseDuration: 0, condition: "running_style_temptation_opponent_count_oikomi>=1&is_temptation==0", effects: [{ modifier: 5e4, target: 21, type: 13 }], precondition: "" }], rarity: 1 }, "200831": { alternatives: [{ baseDuration: 0, condition: "running_style_count_nige_otherself>=1&phase_random==0&accumulatetime>=5", effects: [{ modifier: -100, target: 18, type: 9 }], precondition: "" }], rarity: 1 }, "200841": { alternatives: [{ baseDuration: 0, condition: "running_style_count_nige_otherself>=1&phase_random==1", effects: [{ modifier: -100, target: 18, type: 9 }], precondition: "" }], rarity: 1 }, "200851": { alternatives: [{ baseDuration: 3e4, condition: "running_style_count_nige_otherself>=1&phase_random==2", effects: [{ modifier: -1500, target: 18, type: 21 }], precondition: "" }], rarity: 1 }, "200861": { alternatives: [{ baseDuration: 0, condition: "running_style_count_senko_otherself>=1&phase_random==0&accumulatetime>=5", effects: [{ modifier: -100, target: 18, type: 9 }], precondition: "" }], rarity: 1 }, "200871": { alternatives: [{ baseDuration: 0, condition: "running_style_count_senko_otherself>=1&phase_random==1", effects: [{ modifier: -100, target: 18, type: 9 }], precondition: "" }], rarity: 1 }, "200881": { alternatives: [{ baseDuration: 3e4, condition: "running_style_count_senko_otherself>=1&phase_random==2", effects: [{ modifier: -1500, target: 18, type: 21 }], precondition: "" }], rarity: 1 }, "200891": { alternatives: [{ baseDuration: 0, condition: "running_style_count_sashi_otherself>=1&phase_random==0&accumulatetime>=5", effects: [{ modifier: -100, target: 18, type: 9 }], precondition: "" }], rarity: 1 }, "200901": { alternatives: [{ baseDuration: 0, condition: "running_style_count_sashi_otherself>=1&phase_random==1", effects: [{ modifier: -100, target: 18, type: 9 }], precondition: "" }], rarity: 1 }, "200911": { alternatives: [{ baseDuration: 3e4, condition: "running_style_count_sashi_otherself>=1&phase_random==2", effects: [{ modifier: -1500, target: 18, type: 21 }], precondition: "" }], rarity: 1 }, "200921": { alternatives: [{ baseDuration: 0, condition: "running_style_count_oikomi_otherself>=1&phase_random==0&accumulatetime>=5", effects: [{ modifier: -100, target: 18, type: 9 }], precondition: "" }], rarity: 1 }, "200931": { alternatives: [{ baseDuration: 0, condition: "running_style_count_oikomi_otherself>=1&phase_random==1", effects: [{ modifier: -100, target: 18, type: 9 }], precondition: "" }], rarity: 1 }, "200941": { alternatives: [{ baseDuration: 3e4, condition: "running_style_count_oikomi_otherself>=1&phase_random==2", effects: [{ modifier: -1500, target: 18, type: 21 }], precondition: "" }], rarity: 1 }, "200951": { alternatives: [{ baseDuration: -1, condition: "track_id==10101", effects: [{ modifier: 6e5, target: 1, type: 2 }], precondition: "" }], rarity: 1 }, "200952": { alternatives: [{ baseDuration: -1, condition: "track_id==10101", effects: [{ modifier: 4e5, target: 1, type: 2 }], precondition: "" }], rarity: 1 }, "200953": { alternatives: [{ baseDuration: -1, condition: "track_id==10101", effects: [{ modifier: -4e5, target: 1, type: 2 }], precondition: "" }], rarity: 1 }, "200961": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==1&straight_random==1", effects: [{ modifier: 2500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "200962": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==1&straight_random==1", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "200963": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==1&straight_random==1", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 2 }, "200971": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==1&all_corner_random==1", effects: [{ modifier: 2500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "200972": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==1&all_corner_random==1", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "200981": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==1&phase==1&bashin_diff_behind>=3&order==1", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 2 }, "200982": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==1&phase==1&bashin_diff_behind>=3&order==1", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "200991": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==1&phase_laterhalf_random==1&order>=2&order_rate<=50", effects: [{ modifier: 4e3, target: 1, type: 31 }], precondition: "" }], rarity: 2 }, "200992": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==1&phase_laterhalf_random==1&order>=2&order_rate<=50", effects: [{ modifier: 2e3, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "201001": { alternatives: [{ baseDuration: 4e4, condition: "distance_type==1&phase_random==1", effects: [{ modifier: 350, target: 1, type: 28 }, { modifier: 3e3, target: 1, type: 31 }], precondition: "" }], rarity: 2 }, "201002": { alternatives: [{ baseDuration: 4e4, condition: "distance_type==1&phase_random==1", effects: [{ modifier: 250, target: 1, type: 28 }, { modifier: 2e3, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "201011": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==1&phase_random==0&order_rate<=50&accumulatetime>=5", effects: [{ modifier: -2500, target: 10, type: 21 }], precondition: "" }], rarity: 2 }, "201012": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==1&phase_random==0&order_rate<=50&accumulatetime>=5", effects: [{ modifier: -2e3, target: 10, type: 21 }], precondition: "" }], rarity: 1 }, "201021": { alternatives: [{ baseDuration: 12e3, condition: "distance_type==1&phase_random==0&order_rate>50&accumulatetime>=5", effects: [{ modifier: -300, target: 9, type: 9 }, { modifier: -2e3, target: 9, type: 31 }], precondition: "" }], rarity: 2 }, "201022": { alternatives: [{ baseDuration: 12e3, condition: "distance_type==1&phase_random==0&order_rate>50&accumulatetime>=5", effects: [{ modifier: -100, target: 9, type: 9 }, { modifier: -500, target: 9, type: 31 }], precondition: "" }], rarity: 1 }, "201031": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==2&straight_random==1", effects: [{ modifier: 2500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "201032": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==2&straight_random==1", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "201041": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==2&all_corner_random==1", effects: [{ modifier: 2500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "201042": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==2&all_corner_random==1", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "201051": { alternatives: [{ baseDuration: 24e3, condition: "distance_type==2&phase_random==1&order_rate<=50", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 2 }, "201052": { alternatives: [{ baseDuration: 24e3, condition: "distance_type==2&phase_random==1&order_rate<=50", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "201061": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==2&distance_rate>=50&change_order_onetime<0", effects: [{ modifier: 4e3, target: 1, type: 31 }], precondition: "" }], rarity: 2 }, "201062": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==2&distance_rate>=50&change_order_onetime<0", effects: [{ modifier: 2e3, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "201071": { alternatives: [{ baseDuration: 24e3, condition: "distance_type==2&is_overtake==1&accumulatetime>=5", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 2 }, "201072": { alternatives: [{ baseDuration: 24e3, condition: "distance_type==2&is_overtake==1&accumulatetime>=5", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "201081": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==2&phase_random==1&order<=3", effects: [{ modifier: -2e3, target: 10, type: 21 }, { modifier: 2500, target: 1, type: 27 }], precondition: "" }], rarity: 2 }, "201082": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==2&phase_random==1&order<=3", effects: [{ modifier: -1500, target: 10, type: 21 }, { modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "201091": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==2&phase_random==0&order_rate>50&accumulatetime>=3", effects: [{ modifier: -3e3, target: 9, type: 31 }], precondition: "" }], rarity: 2 }, "201092": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==2&phase_random==0&order_rate>50&accumulatetime>=3", effects: [{ modifier: -1e3, target: 9, type: 31 }], precondition: "" }], rarity: 1 }, "201101": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==3&straight_random==1", effects: [{ modifier: 2500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "201102": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==3&straight_random==1", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "201103": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==3&straight_random==1", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 2 }, "201111": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==3&all_corner_random==1", effects: [{ modifier: 2500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "201112": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==3&all_corner_random==1", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "201113": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==3&all_corner_random==1", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 2 }, "201121": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==3&phase_random==0", effects: [{ modifier: 15e4, target: 1, type: 8 }], precondition: "" }], rarity: 2 }, "201122": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==3&phase_random==0", effects: [{ modifier: 1e5, target: 1, type: 8 }], precondition: "" }], rarity: 1 }, "201131": { alternatives: [{ baseDuration: 4e4, condition: "distance_type==3&phase_random==1", effects: [{ modifier: 350, target: 1, type: 28 }, { modifier: 3e3, target: 1, type: 31 }], precondition: "" }], rarity: 2 }, "201132": { alternatives: [{ baseDuration: 4e4, condition: "distance_type==3&phase_random==1", effects: [{ modifier: 250, target: 1, type: 28 }, { modifier: 2e3, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "201141": { alternatives: [{ baseDuration: 0, condition: "distance_type==3&is_move_lane==1&accumulatetime>=10@distance_type==3&is_move_lane==2&accumulatetime>=10", effects: [{ modifier: 550, target: 1, type: 9 }], precondition: "" }], rarity: 2 }, "201142": { alternatives: [{ baseDuration: 0, condition: "distance_type==3&is_move_lane==1&accumulatetime>=10@distance_type==3&is_move_lane==2&accumulatetime>=10", effects: [{ modifier: 150, target: 1, type: 9 }], precondition: "" }], rarity: 1 }, "201151": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==3&phase_random==2&order_rate>50", effects: [{ modifier: -2500, target: 9, type: 21 }], precondition: "" }], rarity: 2 }, "201152": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==3&phase_random==2&order_rate>50", effects: [{ modifier: -1500, target: 9, type: 21 }], precondition: "" }], rarity: 1 }, "201161": { alternatives: [{ baseDuration: 0, condition: "distance_type==3&phase==1&blocked_front_continuetime>=1", effects: [{ modifier: -300, target: 9, type: 9 }], precondition: "" }], rarity: 2 }, "201162": { alternatives: [{ baseDuration: 0, condition: "distance_type==3&phase==1&blocked_front_continuetime>=1", effects: [{ modifier: -100, target: 9, type: 9 }], precondition: "" }], rarity: 1 }, "201171": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==4&straight_random==1", effects: [{ modifier: 2500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "201172": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==4&straight_random==1", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "201173": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==4&straight_random==1", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 2 }, "201181": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==4&all_corner_random==1", effects: [{ modifier: 2500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "201182": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==4&all_corner_random==1", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "201191": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==4&phase_random==1&bashin_diff_behind>=1&order==1", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 2 }, "201192": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==4&phase_random==1&bashin_diff_behind>=1&order==1", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "201201": { alternatives: [{ baseDuration: 0, condition: "distance_type==4&is_overtake==1&accumulatetime>=5", effects: [{ modifier: 550, target: 1, type: 9 }], precondition: "" }], rarity: 2 }, "201202": { alternatives: [{ baseDuration: 0, condition: "distance_type==4&is_overtake==1&accumulatetime>=5", effects: [{ modifier: 150, target: 1, type: 9 }], precondition: "" }], rarity: 1 }, "201211": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==4&phase==2&change_order_onetime<0", effects: [{ modifier: 3500, target: 1, type: 27 }, { modifier: 1e3, target: 1, type: 31 }], precondition: "" }], rarity: 2 }, "201212": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==4&phase==2&change_order_onetime<0", effects: [{ modifier: 1500, target: 1, type: 27 }, { modifier: 500, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "201221": { alternatives: [{ baseDuration: 0, condition: "distance_type==4&phase_random==1&order>=5", effects: [{ modifier: -100, target: 9, type: 9 }, { modifier: 350, target: 1, type: 9 }], precondition: "" }], rarity: 2 }, "201222": { alternatives: [{ baseDuration: 0, condition: "distance_type==4&phase_random==1&order>=5", effects: [{ modifier: -50, target: 9, type: 9 }, { modifier: 150, target: 1, type: 9 }], precondition: "" }], rarity: 1 }, "201231": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==4&phase_random==2", effects: [{ modifier: -1e5, target: 9, type: 8 }], precondition: "" }], rarity: 2 }, "201232": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==4&phase_random==2", effects: [{ modifier: -5e4, target: 9, type: 8 }], precondition: "" }], rarity: 1 }, "201241": { alternatives: [{ baseDuration: 3e4, condition: "running_style==1&straight_random==1", effects: [{ modifier: 2500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "201242": { alternatives: [{ baseDuration: 3e4, condition: "running_style==1&straight_random==1", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "201251": { alternatives: [{ baseDuration: 3e4, condition: "running_style==1&all_corner_random==1", effects: [{ modifier: 2500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "201252": { alternatives: [{ baseDuration: 3e4, condition: "running_style==1&all_corner_random==1", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "201261": { alternatives: [{ baseDuration: 3e4, condition: "running_style==1&phase==0&blocked_front_continuetime>=1@running_style==1&phase==0&blocked_side_continuetime>=1", effects: [{ modifier: 350, target: 1, type: 28 }, { modifier: 5e3, target: 1, type: 35 }], precondition: "" }], rarity: 2 }, "201262": { alternatives: [{ baseDuration: 3e4, condition: "running_style==1&phase==0&blocked_front_continuetime>=1@running_style==1&phase==0&blocked_side_continuetime>=1", effects: [{ modifier: 250, target: 1, type: 28 }, { modifier: 5e3, target: 1, type: 35 }], precondition: "" }], rarity: 1 }, "201271": { alternatives: [{ baseDuration: 3e4, condition: "running_style==1&phase<=1&change_order_onetime>0&accumulatetime>=5@running_style==1&phase<=1&blocked_side_continuetime>=2&accumulatetime>=5", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 2 }, "201272": { alternatives: [{ baseDuration: 3e4, condition: "running_style==1&phase<=1&change_order_onetime>0&accumulatetime>=5@running_style==1&phase<=1&blocked_side_continuetime>=2&accumulatetime>=5", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "201281": { alternatives: [{ baseDuration: 0, condition: "running_style==1&slope==1&accumulatetime>=10", effects: [{ modifier: 550, target: 1, type: 9 }], precondition: "" }], rarity: 2 }, "201282": { alternatives: [{ baseDuration: 0, condition: "running_style==1&slope==1&accumulatetime>=10", effects: [{ modifier: 150, target: 1, type: 9 }], precondition: "" }], rarity: 1 }, "201291": { alternatives: [{ baseDuration: 3e4, condition: "running_style==1&phase_random==2", effects: [{ modifier: 4e3, target: 1, type: 31 }, { modifier: -200, target: 1, type: 9 }], precondition: "" }], rarity: 2 }, "201292": { alternatives: [{ baseDuration: 3e4, condition: "running_style==1&phase_random==2", effects: [{ modifier: 2e3, target: 1, type: 31 }, { modifier: -200, target: 1, type: 9 }], precondition: "" }], rarity: 1 }, "201302": { alternatives: [{ baseDuration: 3e4, condition: "running_style==1&phase_random==0&order>=2&accumulatetime>=5", effects: [{ modifier: -1e3, target: 9, type: 31 }], precondition: "" }], rarity: 1 }, "201311": { alternatives: [{ baseDuration: 3e4, condition: "running_style==2&straight_random==1", effects: [{ modifier: 2500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "201312": { alternatives: [{ baseDuration: 3e4, condition: "running_style==2&straight_random==1", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "201321": { alternatives: [{ baseDuration: 3e4, condition: "running_style==2&all_corner_random==1", effects: [{ modifier: 2500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "201322": { alternatives: [{ baseDuration: 3e4, condition: "running_style==2&all_corner_random==1", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "201331": { alternatives: [{ baseDuration: 3e4, condition: "running_style==2&is_move_lane==1@running_style==2&is_move_lane==2", effects: [{ modifier: 3e3, target: 1, type: 31 }], precondition: "" }], rarity: 2 }, "201332": { alternatives: [{ baseDuration: 3e4, condition: "running_style==2&is_move_lane==1@running_style==2&is_move_lane==2", effects: [{ modifier: 2e3, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "201341": { alternatives: [{ baseDuration: 3e4, condition: "running_style==2&down_slope_random==1", effects: [{ modifier: 3e3, target: 1, type: 31 }], precondition: "" }], rarity: 2 }, "201342": { alternatives: [{ baseDuration: 3e4, condition: "running_style==2&down_slope_random==1", effects: [{ modifier: 2e3, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "201351": { alternatives: [{ baseDuration: 0, condition: "running_style==2&phase_random==1", effects: [{ modifier: 550, target: 1, type: 9 }], precondition: "" }], rarity: 2 }, "201352": { alternatives: [{ baseDuration: 0, condition: "running_style==2&phase_random==1", effects: [{ modifier: 150, target: 1, type: 9 }], precondition: "" }], rarity: 1 }, "201361": { alternatives: [{ baseDuration: 3e4, condition: "running_style==2&phase_random==1&order_rate>40", effects: [{ modifier: 3e3, target: 1, type: 31 }], precondition: "" }], rarity: 2 }, "201362": { alternatives: [{ baseDuration: 3e4, condition: "running_style==2&phase_random==1&order_rate>40", effects: [{ modifier: 2e3, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "201371": { alternatives: [{ baseDuration: 3e4, condition: "running_style==2&phase_random==2&order_rate<=50", effects: [{ modifier: -5e4, target: 10, type: 8 }], precondition: "" }], rarity: 2 }, "201372": { alternatives: [{ baseDuration: 3e4, condition: "running_style==2&phase_random==2&order_rate<=50", effects: [{ modifier: -3e4, target: 10, type: 8 }], precondition: "" }], rarity: 1 }, "201381": { alternatives: [{ baseDuration: 3e4, condition: "running_style==3&straight_random==1", effects: [{ modifier: 2500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "201382": { alternatives: [{ baseDuration: 3e4, condition: "running_style==3&straight_random==1", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "201391": { alternatives: [{ baseDuration: 3e4, condition: "running_style==3&all_corner_random==1", effects: [{ modifier: 2500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "201392": { alternatives: [{ baseDuration: 3e4, condition: "running_style==3&all_corner_random==1", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "201401": { alternatives: [{ baseDuration: 4e4, condition: "running_style==3&is_overtake==1&accumulatetime>=5", effects: [{ modifier: 3e3, target: 1, type: 31 }], precondition: "" }], rarity: 2 }, "201402": { alternatives: [{ baseDuration: 4e4, condition: "running_style==3&is_overtake==1&accumulatetime>=5", effects: [{ modifier: 2e3, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "201411": { alternatives: [{ baseDuration: 24e3, condition: "running_style==3&up_slope_random==1", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 2 }, "201412": { alternatives: [{ baseDuration: 24e3, condition: "running_style==3&up_slope_random==1", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "201421": { alternatives: [{ baseDuration: 0, condition: "running_style==3&phase_random==1&order_rate>=40", effects: [{ modifier: 550, target: 1, type: 9 }], precondition: "" }], rarity: 2 }, "201422": { alternatives: [{ baseDuration: 0, condition: "running_style==3&phase_random==1&order_rate>=40", effects: [{ modifier: 150, target: 1, type: 9 }], precondition: "" }], rarity: 1 }, "201431": { alternatives: [{ baseDuration: 3e4, condition: "running_style==3&phase_random==1", effects: [{ modifier: 15e4, target: 1, type: 8 }], precondition: "" }], rarity: 2 }, "201432": { alternatives: [{ baseDuration: 3e4, condition: "running_style==3&phase_random==1", effects: [{ modifier: 5e4, target: 1, type: 8 }], precondition: "" }], rarity: 1 }, "201441": { alternatives: [{ baseDuration: 0, condition: "running_style==3&phase_random==2&order_rate>50", effects: [{ modifier: -300, target: 4, type: 9 }], precondition: "" }], rarity: 2 }, "201442": { alternatives: [{ baseDuration: 0, condition: "running_style==3&phase_random==2&order_rate>50", effects: [{ modifier: -100, target: 4, type: 9 }], precondition: "" }], rarity: 1 }, "201451": { alternatives: [{ baseDuration: 3e4, condition: "running_style==4&straight_random==1", effects: [{ modifier: 2500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "201452": { alternatives: [{ baseDuration: 3e4, condition: "running_style==4&straight_random==1", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "201453": { alternatives: [{ baseDuration: 3e4, condition: "running_style==4&straight_random==1", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 2 }, "201461": { alternatives: [{ baseDuration: 3e4, condition: "running_style==4&all_corner_random==1", effects: [{ modifier: 2500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "201462": { alternatives: [{ baseDuration: 3e4, condition: "running_style==4&all_corner_random==1", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "201471": { alternatives: [{ baseDuration: 3e4, condition: "running_style==4&is_move_lane==1@running_style==4&is_move_lane==2", effects: [{ modifier: 1e5, target: 1, type: 8 }], precondition: "" }], rarity: 2 }, "201472": { alternatives: [{ baseDuration: 3e4, condition: "running_style==4&is_move_lane==1@running_style==4&is_move_lane==2", effects: [{ modifier: 5e4, target: 1, type: 8 }], precondition: "" }], rarity: 1 }, "201481": { alternatives: [{ baseDuration: 0, condition: "running_style==4&slope==2&accumulatetime>=10", effects: [{ modifier: 550, target: 1, type: 9 }], precondition: "" }], rarity: 2 }, "201482": { alternatives: [{ baseDuration: 0, condition: "running_style==4&slope==2&accumulatetime>=10", effects: [{ modifier: 150, target: 1, type: 9 }], precondition: "" }], rarity: 1 }, "201491": { alternatives: [{ baseDuration: 0, condition: "running_style==4&infront_near_lane_time>=1&accumulatetime>=10", effects: [{ modifier: 550, target: 1, type: 9 }], precondition: "" }], rarity: 2 }, "201492": { alternatives: [{ baseDuration: 0, condition: "running_style==4&infront_near_lane_time>=1&accumulatetime>=10", effects: [{ modifier: 150, target: 1, type: 9 }], precondition: "" }], rarity: 1 }, "201501": { alternatives: [{ baseDuration: 3e4, condition: "running_style==4&phase_random==2&order_rate>50", effects: [{ modifier: 15e4, target: 1, type: 8 }], precondition: "" }], rarity: 2 }, "201502": { alternatives: [{ baseDuration: 3e4, condition: "running_style==4&phase_random==2&order_rate>50", effects: [{ modifier: 5e4, target: 1, type: 8 }], precondition: "" }], rarity: 1 }, "201511": { alternatives: [{ baseDuration: 3e4, condition: "running_style==4&phase_random==2&order>=2", effects: [{ modifier: -2500, target: 4, type: 21 }], precondition: "" }], rarity: 2 }, "201512": { alternatives: [{ baseDuration: 3e4, condition: "running_style==4&phase_random==2&order>=2", effects: [{ modifier: -1500, target: 4, type: 21 }], precondition: "" }], rarity: 1 }, "201521": { alternatives: [{ baseDuration: -1, condition: "running_style==1", effects: [{ modifier: 6e5, target: 1, type: 5 }, { modifier: 1e5, target: 1, type: 8 }], precondition: "" }], rarity: 1 }, "201522": { alternatives: [{ baseDuration: -1, condition: "running_style==1", effects: [{ modifier: 4e5, target: 1, type: 5 }, { modifier: 5e4, target: 1, type: 8 }], precondition: "" }], rarity: 1 }, "201531": { alternatives: [{ baseDuration: -1, condition: "running_style==2", effects: [{ modifier: 6e5, target: 1, type: 5 }, { modifier: 1e5, target: 1, type: 8 }], precondition: "" }], rarity: 1 }, "201532": { alternatives: [{ baseDuration: -1, condition: "running_style==2", effects: [{ modifier: 4e5, target: 1, type: 5 }, { modifier: 5e4, target: 1, type: 8 }], precondition: "" }], rarity: 1 }, "201541": { alternatives: [{ baseDuration: -1, condition: "running_style==3", effects: [{ modifier: 6e5, target: 1, type: 5 }, { modifier: 1e5, target: 1, type: 8 }], precondition: "" }], rarity: 1 }, "201542": { alternatives: [{ baseDuration: -1, condition: "running_style==3", effects: [{ modifier: 4e5, target: 1, type: 5 }, { modifier: 5e4, target: 1, type: 8 }], precondition: "" }], rarity: 1 }, "201551": { alternatives: [{ baseDuration: -1, condition: "running_style==4", effects: [{ modifier: 6e5, target: 1, type: 5 }, { modifier: 1e5, target: 1, type: 8 }], precondition: "" }], rarity: 1 }, "201552": { alternatives: [{ baseDuration: -1, condition: "running_style==4", effects: [{ modifier: 4e5, target: 1, type: 5 }, { modifier: 5e4, target: 1, type: 8 }], precondition: "" }], rarity: 1 }, "201561": { alternatives: [{ baseDuration: -1, condition: "random_lot==50&post_number==7", effects: [{ modifier: 6e5, target: 1, type: 1 }, { modifier: 6e5, target: 1, type: 2 }, { modifier: 6e5, target: 1, type: 3 }], precondition: "" }], rarity: 2 }, "201562": { alternatives: [{ baseDuration: -1, condition: "random_lot==50&post_number==7", effects: [{ modifier: 4e5, target: 1, type: 1 }, { modifier: 4e5, target: 1, type: 2 }, { modifier: 4e5, target: 1, type: 3 }], precondition: "" }], rarity: 1 }, "201571": { alternatives: [{ baseDuration: 0, condition: "remain_distance<=778&remain_distance>=776", effects: [{ modifier: 150, target: 1, type: 9 }], precondition: "" }], rarity: 1 }, "201581": { alternatives: [{ baseDuration: 3e4, condition: "up_slope_random==1", effects: [{ modifier: 2e3, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "201591": { alternatives: [{ baseDuration: 3e4, condition: "near_count>=3&accumulatetime>=5", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "201592": { alternatives: [{ baseDuration: 3e4, condition: "near_count>=3&accumulatetime>=5", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 2 }, "201601": { alternatives: [{ baseDuration: 3e4, condition: "activate_count_start>=3", effects: [{ modifier: 2e3, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "201611": { alternatives: [{ baseDuration: 3e4, condition: "activate_count_middle>=3", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "201612": { alternatives: [{ baseDuration: 3e4, condition: "activate_count_middle>=3", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 2 }, "201621": { alternatives: [{ baseDuration: 0, condition: "activate_count_end_after>=3", effects: [{ modifier: 150, target: 1, type: 9 }], precondition: "" }], rarity: 1 }, "201631": { alternatives: [{ baseDuration: -1, condition: "same_skill_horse_count>=5", effects: [{ modifier: 4e5, target: 1, type: 1 }], precondition: "" }], rarity: 1 }, "201641": { alternatives: [{ baseDuration: -1, condition: "same_skill_horse_count==1", effects: [{ modifier: 4e5, target: 1, type: 1 }], precondition: "" }], rarity: 1 }, "201651": { alternatives: [{ baseDuration: 3e4, condition: "infront_near_lane_time>=3&accumulatetime>=10", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "201652": { alternatives: [{ baseDuration: 3e4, condition: "infront_near_lane_time>=3&accumulatetime>=10", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 2 }, "201661": { alternatives: [{ baseDuration: 3e4, condition: "behind_near_lane_time>=3&accumulatetime>=10", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "201662": { alternatives: [{ baseDuration: 3e4, condition: "behind_near_lane_time>=3&accumulatetime>=10", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 2 }, "201671": { alternatives: [{ baseDuration: 24e3, condition: "ground_type==2&phase==1&blocked_side_continuetime>=2", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 2 }, "201672": { alternatives: [{ baseDuration: 24e3, condition: "ground_type==2&phase==1&blocked_side_continuetime>=2", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "201681": { alternatives: [{ baseDuration: 3e4, condition: "ground_type==2&phase_random==2", effects: [{ modifier: 350, target: 1, type: 28 }, { modifier: 3e3, target: 1, type: 31 }], precondition: "" }], rarity: 2 }, "201682": { alternatives: [{ baseDuration: 3e4, condition: "ground_type==2&phase_random==2", effects: [{ modifier: 250, target: 1, type: 28 }, { modifier: 2e3, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "201691": { alternatives: [{ baseDuration: 0, condition: "running_style==3&phase_laterhalf_random==0&order_rate>=40", effects: [{ modifier: 550, target: 1, type: 9 }], precondition: "" }], rarity: 2 }, "201692": { alternatives: [{ baseDuration: 0, condition: "running_style==3&phase_laterhalf_random==0&order_rate>=40", effects: [{ modifier: 150, target: 1, type: 9 }], precondition: "" }], rarity: 1 }, "201701": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==3&is_lastspurt==1&corner==0&order_rate>=20&order_rate<=60", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 2 }, "201702": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==3&is_lastspurt==1&corner==0&order_rate>=20&order_rate<=60", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "201801": { alternatives: [{ baseDuration: -1, condition: "ground_condition==1", effects: [{ modifier: -4e5, target: 1, type: 1 }], precondition: "" }], rarity: 1 }, "201901": { alternatives: [{ baseDuration: 18e3, condition: "running_style==2&phase_firsthalf_random==2&order_rate<=50", effects: [{ modifier: 4e3, target: 1, type: 31 }], precondition: "" }], rarity: 2 }, "201902": { alternatives: [{ baseDuration: 18e3, condition: "running_style==2&phase_firsthalf_random==2&order_rate<=50", effects: [{ modifier: 2e3, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "202001": { alternatives: [{ baseDuration: 18e3, condition: "ground_type==2&phase_random==1&order_rate>=50", effects: [{ modifier: 550, target: 1, type: 9 }, { modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 2 }, "202002": { alternatives: [{ baseDuration: 0, condition: "ground_type==2&phase_random==1&order_rate>=50", effects: [{ modifier: 150, target: 1, type: 9 }], precondition: "" }], rarity: 1 }, "202011": { alternatives: [{ baseDuration: 24e3, condition: "distance_type==4&phase_laterhalf_random==1&order_rate<=50", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 2 }, "202012": { alternatives: [{ baseDuration: 24e3, condition: "distance_type==4&phase_laterhalf_random==1&order_rate<=50", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "202021": { alternatives: [{ baseDuration: 4e4, condition: "running_style==4&phase_random==1&order_rate>=50", effects: [{ modifier: 2500, target: 1, type: 27 }], precondition: "" }], rarity: 2 }, "202022": { alternatives: [{ baseDuration: 4e4, condition: "running_style==4&phase_random==1&order_rate>=50", effects: [{ modifier: 500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "202031": { alternatives: [{ baseDuration: 18e3, condition: "distance_rate_after_random==50", effects: [{ modifier: 4500, target: 1, type: 27 }, { modifier: -1e4, target: 1, type: 9 }], precondition: "" }], rarity: 2 }, "202032": { alternatives: [{ baseDuration: 18e3, condition: "distance_rate_after_random==50", effects: [{ modifier: 2500, target: 1, type: 27 }, { modifier: -1e4, target: 1, type: 9 }], precondition: "" }], rarity: 1 }, "202041": { alternatives: [{ baseDuration: 24e3, condition: "distance_type==1&phase_laterhalf_random==1&order_rate<=50", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 2 }, "202042": { alternatives: [{ baseDuration: 24e3, condition: "distance_type==1&phase_laterhalf_random==1&order_rate<=50", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "202051": { alternatives: [{ baseDuration: -1, condition: "running_style==1", effects: [{ modifier: 0, target: 1, type: 6 }], precondition: "" }], rarity: 2 }, "202061": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==4&is_finalcorner_random==1", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 2 }, "202071": { alternatives: [{ baseDuration: 12e3, condition: "distance_type==4&phase_firsthalf_random==1&order_rate>=40&order_rate<=80", effects: [{ modifier: 750, target: 1, type: 9 }, { modifier: -1500, target: 1, type: 21 }], precondition: "" }], rarity: 2 }, "202072": { alternatives: [{ baseDuration: 12e3, condition: "distance_type==4&phase_firsthalf_random==1&order_rate>=40&order_rate<=80", effects: [{ modifier: 350, target: 1, type: 9 }, { modifier: -1500, target: 1, type: 21 }], precondition: "" }], rarity: 1 }, "202081": { alternatives: [{ baseDuration: 18e3, condition: "distance_type==3&phase_firsthalf_random==2&order_rate>=40", effects: [{ modifier: 4e3, target: 1, type: 31 }], precondition: "" }], rarity: 2 }, "202082": { alternatives: [{ baseDuration: 18e3, condition: "distance_type==3&phase_firsthalf_random==2&order_rate>=40", effects: [{ modifier: 2e3, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "202091": { alternatives: [{ baseDuration: 24e3, condition: "distance_type==3&phase==1&order_rate<=80&order_rate>=30&blocked_side_continuetime>=2", effects: [{ modifier: 3500, target: 1, type: 27 }, { modifier: 350, target: 1, type: 9 }], precondition: "" }], rarity: 2 }, "202092": { alternatives: [{ baseDuration: 24e3, condition: "distance_type==3&phase==1&order_rate<=80&order_rate>=30&blocked_side_continuetime>=2", effects: [{ modifier: 1500, target: 1, type: 27 }, { modifier: 50, target: 1, type: 9 }], precondition: "" }], rarity: 1 }, "202101": { alternatives: [{ baseDuration: 24e3, condition: "distance_type==3&phase_random==1&order_rate>=50", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 2 }, "202102": { alternatives: [{ baseDuration: 24e3, condition: "distance_type==3&phase_random==1&order_rate>=50", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "202111": { alternatives: [{ baseDuration: 18e3, condition: "distance_type==2&is_finalcorner_random==1&order_rate>=50", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 2 }, "202112": { alternatives: [{ baseDuration: 18e3, condition: "distance_type==2&is_finalcorner_random==1&order_rate>=50", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "202121": { alternatives: [{ baseDuration: 24e3, condition: "running_style==3&distance_rate_after_random==50&order_rate>=30&order_rate<=80", effects: [{ modifier: 3500, target: 1, type: 27 }, { modifier: 1e3, target: 1, type: 31 }], precondition: "" }], rarity: 2 }, "202122": { alternatives: [{ baseDuration: 24e3, condition: "running_style==3&distance_rate_after_random==50&order_rate>=30&order_rate<=80", effects: [{ modifier: 1500, target: 1, type: 27 }, { modifier: 500, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "202131": { alternatives: [{ baseDuration: 18e3, condition: "distance_type==3&phase_random==1&order<=3", effects: [{ modifier: 3500, target: 1, type: 27 }, { modifier: -1500, target: 10, type: 21 }], precondition: "" }], rarity: 2 }, "202132": { alternatives: [{ baseDuration: 18e3, condition: "distance_type==3&phase_random==1&order<=3", effects: [{ modifier: 1500, target: 1, type: 27 }, { modifier: -350, target: 10, type: 21 }], precondition: "" }], rarity: 1 }, "202141": { alternatives: [{ baseDuration: -1, condition: "always==1", effects: [{ modifier: 850, target: 1, type: 14 }, { modifier: -4e5, target: 1, type: 5 }], precondition: "" }], rarity: 1 }, "202151": { alternatives: [{ baseDuration: 24e3, condition: "running_style==3&phase_random==1", effects: [{ modifier: 4500, target: 1, type: 27 }, { modifier: -200, target: 1, type: 9 }], precondition: "" }], rarity: 2 }, "202152": { alternatives: [{ baseDuration: 24e3, condition: "running_style==3&phase_random==1", effects: [{ modifier: 2500, target: 1, type: 27 }, { modifier: -200, target: 1, type: 9 }], precondition: "" }], rarity: 1 }, "202161": { alternatives: [{ baseDuration: -1, condition: "always==1", effects: [{ modifier: 6e5, target: 1, type: 5 }, { modifier: -3e4, target: 1, type: 29 }], precondition: "" }], rarity: 1 }, "202172": { alternatives: [{ baseDuration: 24e3, condition: "down_slope_random==1", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "202181": { alternatives: [{ baseDuration: -1, condition: "always==1", effects: [{ modifier: -4e5, target: 1, type: 4 }, { modifier: -4e5, target: 1, type: 5 }], precondition: "" }], rarity: 1 }, "202191": { alternatives: [{ baseDuration: 0, condition: "is_badstart==0&running_style==2&distance_type==4&phase_firsthalf_random==1", effects: [{ modifier: 750, target: 1, type: 9 }], precondition: "" }], rarity: 2 }, "202192": { alternatives: [{ baseDuration: 0, condition: "is_badstart==0&running_style==2&distance_type==4&phase_firsthalf_random==1", effects: [{ modifier: 350, target: 1, type: 9 }], precondition: "" }], rarity: 1 }, "202251": { alternatives: [{ baseDuration: -1, condition: "is_dirtgrade==1", effects: [{ modifier: 6e5, target: 1, type: 1 }], precondition: "" }], rarity: 1 }, "202252": { alternatives: [{ baseDuration: -1, condition: "is_dirtgrade==1", effects: [{ modifier: 4e5, target: 1, type: 1 }], precondition: "" }], rarity: 1 }, "202261": { alternatives: [{ baseDuration: 24e3, condition: "ground_type==2&phase_laterhalf_random==1&order_rate<=50", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 2 }, "202262": { alternatives: [{ baseDuration: 24e3, condition: "ground_type==2&phase_laterhalf_random==1&order_rate<=50", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "202281": { alternatives: [{ baseDuration: 3e4, condition: "ground_type==2&phase_random==3&order_rate<=50&is_lastspurt==1", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 2 }, "202282": { alternatives: [{ baseDuration: 3e4, condition: "ground_type==2&phase_random==3&order_rate<=50&is_lastspurt==1", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "202301": { alternatives: [{ baseDuration: 3e4, condition: "ground_type==2&accumulatetime>=5&infront_near_lane_time>=3", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 2 }, "202302": { alternatives: [{ baseDuration: 3e4, condition: "ground_type==2&accumulatetime>=5&infront_near_lane_time>=3", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "202311": { alternatives: [{ baseDuration: 3e4, condition: "ground_type==2&phase_random==2&order_rate<=50", effects: [{ modifier: 4e3, target: 1, type: 31 }], precondition: "" }], rarity: 2 }, "202312": { alternatives: [{ baseDuration: 3e4, condition: "ground_type==2&phase_random==2&order_rate<=50", effects: [{ modifier: 2e3, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "202321": { alternatives: [{ baseDuration: 3e4, condition: "ground_type==2&phase_random==2&order_rate>=50", effects: [{ modifier: 4e3, target: 1, type: 31 }], precondition: "" }], rarity: 2 }, "202322": { alternatives: [{ baseDuration: 3e4, condition: "ground_type==2&phase_random==2&order_rate>=50", effects: [{ modifier: 2e3, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "202331": { alternatives: [{ baseDuration: -1, condition: "ground_type==2&base_power>=1200", effects: [{ modifier: 8e5, target: 1, type: 1 }], precondition: "" }, { baseDuration: -1, condition: "ground_type==2&base_power>=1000&base_power<1200", effects: [{ modifier: 6e5, target: 1, type: 1 }], precondition: "" }], rarity: 2 }, "202332": { alternatives: [{ baseDuration: -1, condition: "ground_type==2&base_power>=1200", effects: [{ modifier: 4e5, target: 1, type: 1 }], precondition: "" }, { baseDuration: -1, condition: "ground_type==2&base_power>=1000&base_power<1200", effects: [{ modifier: 2e5, target: 1, type: 1 }], precondition: "" }], rarity: 1 }, "202341": { alternatives: [{ baseDuration: -1, condition: "ground_type==2&ground_condition==3@ground_type==2&ground_condition==4", effects: [{ modifier: 6e5, target: 1, type: 1 }, { modifier: 6e5, target: 1, type: 3 }], precondition: "" }], rarity: 2 }, "202342": { alternatives: [{ baseDuration: -1, condition: "ground_type==2&ground_condition==3@ground_type==2&ground_condition==4", effects: [{ modifier: 6e5, target: 1, type: 1 }], precondition: "" }], rarity: 1 }, "202343": { alternatives: [{ baseDuration: -1, condition: "ground_type==2&ground_condition==3@ground_type==2&ground_condition==4", effects: [{ modifier: 4e5, target: 1, type: 1 }], precondition: "" }], rarity: 1 }, "202351": { alternatives: [{ baseDuration: 3e4, condition: "ground_type==2&accumulatetime>=10&phase==0&order<=1&bashin_diff_behind<=1", effects: [{ modifier: -2500, target: 10, type: 21 }], precondition: "" }], rarity: 2 }, "202352": { alternatives: [{ baseDuration: 3e4, condition: "ground_type==2&accumulatetime>=10&phase==0&order<=1&bashin_diff_behind<=1", effects: [{ modifier: -1500, target: 10, type: 21 }], precondition: "" }], rarity: 1 }, "202361": { alternatives: [{ baseDuration: 3e4, condition: "ground_type==2&distance_rate>=50&is_other_character_activate_advantage_skill==9", effects: [{ modifier: -2500, target: 23, type: 21 }], precondition: "" }], rarity: 2 }, "202362": { alternatives: [{ baseDuration: 3e4, condition: "ground_type==2&distance_rate>=50&is_other_character_activate_advantage_skill==9", effects: [{ modifier: -1500, target: 23, type: 21 }], precondition: "" }], rarity: 1 }, "202371": { alternatives: [{ baseDuration: 24e3, condition: "phase_random==1&running_style==2", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 2 }, "202372": { alternatives: [{ baseDuration: 24e3, condition: "phase_random==1&running_style==2", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "202381": { alternatives: [{ baseDuration: 24e3, condition: "phase_random==1&running_style==4&order_rate>=75", effects: [{ modifier: 3500, target: 1, type: 27 }, { modifier: 350, target: 1, type: 28 }], precondition: "" }], rarity: 2 }, "202382": { alternatives: [{ baseDuration: 24e3, condition: "phase_random==1&running_style==4&order_rate>=75", effects: [{ modifier: 1500, target: 1, type: 27 }, { modifier: 150, target: 1, type: 28 }], precondition: "" }], rarity: 1 }, "202401": { alternatives: [{ baseDuration: 2e4, condition: "distance_type==1&phase>=2&order_rate>=50&is_overtake==1@distance_type==2&phase>=2&order_rate>=50&is_overtake==1", effects: [{ modifier: 4e3, target: 1, type: 31 }], precondition: "" }], rarity: 2 }, "202402": { alternatives: [{ baseDuration: 2e4, condition: "distance_type==1&phase>=2&order_rate>=50&is_overtake==1@distance_type==2&phase>=2&order_rate>=50&is_overtake==1", effects: [{ modifier: 2e3, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "202411": { alternatives: [{ baseDuration: 4e4, condition: "running_style==2&order>=2&distance_diff_top<=5&distance_rate>=60&phase==1", effects: [{ modifier: 2500, target: 1, type: 27 }], precondition: "" }], rarity: 2 }, "202412": { alternatives: [{ baseDuration: 4e4, condition: "running_style==2&order>=2&distance_diff_top<=5&distance_rate>=60&phase==1", effects: [{ modifier: 500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "202421": { alternatives: [{ baseDuration: 15e3, condition: "distance_type==4&running_style==3&is_lastspurt==1&order_rate>=40", effects: [{ modifier: 4e3, target: 1, type: 31 }, { modifier: -200, target: 1, type: 9 }], precondition: "" }], rarity: 2 }, "202422": { alternatives: [{ baseDuration: 15e3, condition: "distance_type==4&running_style==3&is_lastspurt==1&order_rate>=40", effects: [{ modifier: 2e3, target: 1, type: 31 }, { modifier: -200, target: 1, type: 9 }], precondition: "" }], rarity: 1 }, "202431": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==3&running_style==2&phase_laterhalf_random==1&distance_diff_top<=10", effects: [{ modifier: 2500, target: 1, type: 27 }, { modifier: 3e3, target: 1, type: 31 }], precondition: "" }], rarity: 2 }, "202432": { alternatives: [{ baseDuration: 3e4, condition: "distance_type==3&running_style==2&phase_laterhalf_random==1&distance_diff_top<=10", effects: [{ modifier: 500, target: 1, type: 27 }, { modifier: 1e3, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "202441": { alternatives: [{ baseDuration: -1, condition: "popularity<=3&random_lot==30", effects: [{ modifier: 8e5, target: 1, type: 1 }, { modifier: 8e5, target: 1, type: 3 }, { modifier: 8e5, target: 1, type: 4 }], precondition: "" }, { baseDuration: -1, condition: "popularity>=4&random_lot==60", effects: [{ modifier: 8e5, target: 1, type: 1 }, { modifier: 8e5, target: 1, type: 3 }, { modifier: 8e5, target: 1, type: 4 }], precondition: "" }], rarity: 2 }, "202451": { alternatives: [{ baseDuration: 24e3, condition: "running_style==3&is_finalcorner==1&corner==0&order>=2&distance_diff_top<=10", effects: [{ modifier: 4500, target: 1, type: 27 }], precondition: "" }], rarity: 2 }, "202452": { alternatives: [{ baseDuration: 24e3, condition: "running_style==3&is_finalcorner==1&corner==0&order>=2&distance_diff_top<=10", effects: [{ modifier: 2500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "202461": { alternatives: [{ baseDuration: 24e3, condition: "running_style==1&is_finalcorner==1&corner==0&order==1&bashin_diff_behind<=1", effects: [{ modifier: 4500, target: 1, type: 27 }], precondition: "" }], rarity: 2 }, "202462": { alternatives: [{ baseDuration: 24e3, condition: "running_style==1&is_finalcorner==1&corner==0&order==1&bashin_diff_behind<=1", effects: [{ modifier: 2500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "202471": { alternatives: [{ baseDuration: 24e3, condition: "running_style==2&distance_rate>=50&is_overtake==1@running_style==3&distance_rate>=50&is_overtake==1", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 2 }, "202472": { alternatives: [{ baseDuration: 24e3, condition: "running_style==2&distance_rate>=50&is_overtake==1@running_style==3&distance_rate>=50&is_overtake==1", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "202481": { alternatives: [{ baseDuration: 12e3, condition: "distance_type==4&running_style==2&phase==2", effects: [{ modifier: 4e3, target: 1, type: 31 }], precondition: "distance_diff_top<=10&distance_rate>=60&phase==1" }], rarity: 2 }, "202482": { alternatives: [{ baseDuration: 12e3, condition: "distance_type==4&running_style==2&phase==2", effects: [{ modifier: 2e3, target: 1, type: 31 }], precondition: "distance_diff_top<=10&distance_rate>=60&phase==1" }], rarity: 1 }, "202501": { alternatives: [{ baseDuration: 24e3, condition: "distance_type==2&phase_laterhalf_random==1&order_rate<=50", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 2 }, "202502": { alternatives: [{ baseDuration: 24e3, condition: "distance_type==2&phase_laterhalf_random==1&order_rate<=50", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "210011": { alternatives: [{ baseDuration: 18e3, condition: "phase_random==1", effects: [{ modifier: 4200, target: 1, type: 27 }], precondition: "" }], rarity: 2 }, "210012": { alternatives: [{ baseDuration: 18e3, condition: "phase_random==1", effects: [{ modifier: 1800, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "210021": { alternatives: [{ baseDuration: 0, condition: "phase_random==1", effects: [{ modifier: 660, target: 1, type: 9 }], precondition: "" }], rarity: 2 }, "210022": { alternatives: [{ baseDuration: 0, condition: "phase_random==1", effects: [{ modifier: 180, target: 1, type: 9 }], precondition: "" }], rarity: 1 }, "210031": { alternatives: [{ baseDuration: 12e3, condition: "phase_random==2", effects: [{ modifier: 4800, target: 1, type: 31 }], precondition: "" }], rarity: 2 }, "210032": { alternatives: [{ baseDuration: 12e3, condition: "phase_random==2", effects: [{ modifier: 2400, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "210041": { alternatives: [{ baseDuration: 18e3, condition: "phase_random==2", effects: [{ modifier: 3e3, target: 1, type: 27 }, { modifier: 3600, target: 1, type: 31 }], precondition: "" }], rarity: 2 }, "210042": { alternatives: [{ baseDuration: 18e3, condition: "phase_random==2", effects: [{ modifier: 600, target: 1, type: 27 }, { modifier: 1200, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "210051": { alternatives: [{ baseDuration: 4e4, condition: "phase_random==0", effects: [{ modifier: 420, target: 1, type: 28 }, { modifier: 18e4, target: 1, type: 8 }], precondition: "" }], rarity: 2 }, "210052": { alternatives: [{ baseDuration: 4e4, condition: "phase_random==0", effects: [{ modifier: 180, target: 1, type: 28 }, { modifier: 6e4, target: 1, type: 8 }], precondition: "" }], rarity: 1 }, "210061": { alternatives: [{ baseDuration: 12e3, condition: "distance_rate_after_random==50", effects: [{ modifier: 3e3, target: 1, type: 27 }, { modifier: 3600, target: 1, type: 31 }, { modifier: 420, target: 1, type: 9 }], precondition: "" }], rarity: 2 }, "210062": { alternatives: [{ baseDuration: 12e3, condition: "distance_rate_after_random==50", effects: [{ modifier: 600, target: 1, type: 27 }, { modifier: 1200, target: 1, type: 31 }, { modifier: 60, target: 1, type: 9 }], precondition: "" }], rarity: 1 }, "210081": { alternatives: [{ baseDuration: 24e3, condition: "is_lastspurt==1&phase_random==3", effects: [{ modifier: 4200, target: 1, type: 27 }], precondition: "" }], rarity: 2 }, "210082": { alternatives: [{ baseDuration: 24e3, condition: "is_lastspurt==1&phase_random==3", effects: [{ modifier: 1800, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "210091": { alternatives: [{ baseDuration: 2e4, condition: "phase_laterhalf_random==1", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "210101": { alternatives: [{ baseDuration: 3e4, condition: "phase_random==3&hp_per>=2&is_lastspurt==1", effects: [{ modifier: 2500, target: 1, type: 27 }, { modifier: -200, target: 1, type: 9 }], precondition: "" }], rarity: 1 }, "210111": { alternatives: [{ baseDuration: 4e4, condition: "phase==3&is_overtake==1&is_lastspurt==1", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "210121": { alternatives: [{ baseDuration: 12e3, condition: "compete_fight_count>0", effects: [{ modifier: 2e3, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "210131": { alternatives: [{ baseDuration: 2e4, condition: "activate_count_later_half>=2", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "210141": { alternatives: [{ baseDuration: -1, condition: "motivation>=4", effects: [{ modifier: 4e5, target: 1, type: 2 }, { modifier: 4e5, target: 1, type: 4 }, { modifier: 4e5, target: 1, type: 5 }], precondition: "" }], rarity: 1 }, "300131": { alternatives: [{ baseDuration: 3e4, condition: "remain_distance>=350&remain_distance<=400", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 5 }, "300141": { alternatives: [{ baseDuration: 3e4, condition: "remain_distance>=350&remain_distance<=400", effects: [{ modifier: 3500, target: 1, type: 27 }], precondition: "" }], rarity: 5 }, "900011": { alternatives: [{ baseDuration: 3e4, condition: "phase>=2&order>=1&order_rate<=50&change_order_onetime<0", effects: [{ modifier: 1500, target: 1, type: 22 }, { modifier: 500, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "900021": { alternatives: [{ baseDuration: 3e4, condition: "distance_rate>=50&order==1&bashin_diff_behind>=1", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "900031": { alternatives: [{ baseDuration: 3e4, condition: "is_finalcorner==1&corner==0", effects: [{ modifier: 2500, target: 1, type: 27 }], precondition: "phase>=2&order<=3&bashin_diff_infront<=1&is_overtake==1" }], rarity: 1 }, "900041": { alternatives: [{ baseDuration: 24e3, condition: "is_finalcorner==1&order<=5&order_rate<=50", effects: [{ modifier: 2e3, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "900051": { alternatives: [{ baseDuration: 3e4, condition: "remain_distance<=300", effects: [{ modifier: 2500, target: 1, type: 27 }], precondition: "phase>=2&order_rate<=40&behind_near_lane_time_set1>=1" }], rarity: 1 }, "900061": { alternatives: [{ baseDuration: 3e4, condition: "order>=2&order<=5&order_rate<=50&remain_distance<=201&remain_distance>=199", effects: [{ modifier: 2500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "900071": { alternatives: [{ baseDuration: 36e3, condition: "distance_rate>=50&distance_rate<=60&order_rate>50", effects: [{ modifier: 500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "900081": { alternatives: [{ baseDuration: 3e4, condition: "order>=3&order_rate<=50&remain_distance<=200&bashin_diff_infront<=1@order>=3&order_rate<=50&remain_distance<=200&bashin_diff_behind<=1", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "900091": { alternatives: [{ baseDuration: 3e4, condition: "distance_rate>=50&order==1&bashin_diff_behind<=1@distance_rate>=50&order==2&is_overtake==1", effects: [{ modifier: 500, target: 1, type: 27 }, { modifier: 1e3, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "900101": { alternatives: [{ baseDuration: 24e3, condition: "is_finalcorner_laterhalf==1&corner!=0&order>=3&order_rate<=40", effects: [{ modifier: 2e3, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "900111": { alternatives: [{ baseDuration: 3e4, condition: "is_finalcorner==1&corner==0&change_order_onetime<0&order>=3", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "900121": { alternatives: [{ baseDuration: 3e4, condition: "is_finalcorner==1&corner==0", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "is_finalcorner==1&is_behind_in==1&change_order_onetime<0&order_rate>=40" }], rarity: 1 }, "900131": { alternatives: [{ baseDuration: 3e4, condition: "is_finalcorner==1&corner!=0&distance_diff_rate<=30&distance_type==4&lastspurt==2", effects: [{ modifier: 2500, target: 1, type: 27 }], precondition: "" }, { baseDuration: 3e4, condition: "is_finalcorner==1&corner!=0&distance_diff_rate<=30", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "900141": { alternatives: [{ baseDuration: 3e4, condition: "is_finalcorner==1&corner==0&hp_per>=30&order<=2", effects: [{ modifier: 500, target: 1, type: 27 }, { modifier: 1e3, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "900151": { alternatives: [{ baseDuration: 3e4, condition: "is_finalcorner==1&bashin_diff_behind<=1&order<=4@is_finalcorner==1&bashin_diff_infront<=1&order<=4", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "900161": { alternatives: [{ baseDuration: 3e4, condition: "is_finalcorner==1&order>=2&order_rate<=75&is_behind_in==1&change_order_onetime<0", effects: [{ modifier: 2500, target: 1, type: 27 }], precondition: "phase==1&blocked_side_continuetime>=2" }, { baseDuration: 3e4, condition: "is_finalcorner==1&order>=2&order_rate<=75&is_behind_in==1&change_order_onetime<0", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "900171": { alternatives: [{ baseDuration: 3e4, condition: "is_finalcorner==1&change_order_up_end_after>=3&corner==0", effects: [{ modifier: 2500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "900181": { alternatives: [{ baseDuration: 3e4, condition: "is_finalcorner==1&corner!=0&order>=3&change_order_onetime<0", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "900191": { alternatives: [{ baseDuration: 3e4, condition: "change_order_up_end_after>=2", effects: [{ modifier: 1500, target: 1, type: 27 }, { modifier: 150, target: 1, type: 28 }], precondition: "" }], rarity: 1 }, "900201": { alternatives: [{ baseDuration: 24e3, condition: "phase>=2&corner!=0&order==1", effects: [{ modifier: 2e3, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "900211": { alternatives: [{ baseDuration: 3e4, condition: "distance_rate>=50&corner==0&order_rate>=70&order_rate<=75&is_overtake==1@distance_rate>=50&corner==0&order_rate<=30&order_rate>=20", effects: [{ modifier: 1500, target: 1, type: 27 }, { modifier: 500, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "900221": { alternatives: [{ baseDuration: 3e4, condition: "distance_rate>=50&order>=2&order_rate<=40&blocked_side_continuetime>=2", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "900231": { alternatives: [{ baseDuration: 3e4, condition: "phase>=2&is_finalcorner==1&order<=4&temptation_count==0", effects: [{ modifier: 2500, target: 1, type: 27 }], precondition: "" }, { baseDuration: 3e4, condition: "phase>=2&is_finalcorner==1&order<=4", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "900241": { alternatives: [{ baseDuration: 3e4, condition: "is_finalcorner==1&blocked_side_continuetime>=2&order<=3", effects: [{ modifier: 500, target: 1, type: 27 }, { modifier: 1e3, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "900251": { alternatives: [{ baseDuration: 36e3, condition: "distance_rate>=50&order_rate>=40&order_rate<=70", effects: [{ modifier: 500, target: 1, type: 27 }, { modifier: -250, target: 9, type: 21 }], precondition: "" }], rarity: 1 }, "900261": { alternatives: [{ baseDuration: 3e4, condition: "is_badstart==0&order<=3&is_finalcorner==1&corner==0&order_rate_in20_continue==1", effects: [{ modifier: 2500, target: 1, type: 27 }], precondition: "" }, { baseDuration: 3e4, condition: "is_badstart==0&order<=3&is_finalcorner==1&corner==0", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "900271": { alternatives: [{ baseDuration: 24e3, condition: "phase>=2&corner!=0&order_rate>=65&order_rate<=70", effects: [{ modifier: 2e3, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "900281": { alternatives: [{ baseDuration: 3e4, condition: "distance_rate>=45&distance_rate<=60&order>=3&order_rate<=50&hp_per<=70", effects: [{ modifier: 500, target: 1, type: 27 }, { modifier: 1e3, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "900301": { alternatives: [{ baseDuration: 3e4, condition: "is_finalcorner==1&corner==0", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "phase>=2&order<=4&change_order_onetime<0" }], rarity: 1 }, "900311": { alternatives: [{ baseDuration: 3e4, condition: "remain_distance<=299&remain_distance>=295&order<=2&slope==0@remain_distance<=299&remain_distance>=295&order<=2&slope==2", effects: [{ modifier: 2500, target: 1, type: 27 }], precondition: "remain_distance<=305&remain_distance>=300&slope==1" }, { baseDuration: 3e4, condition: "remain_distance<=299&remain_distance>=295&order<=2", effects: [{ modifier: 500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "900321": { alternatives: [{ baseDuration: 24e3, condition: "distance_rate>=50&corner!=0&order>=3&order_rate<=40", effects: [{ modifier: 150, target: 1, type: 9 }, { modifier: 500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "900331": { alternatives: [{ baseDuration: 3e4, condition: "is_finalcorner==1&corner==0&distance_diff_top>=5&order_rate<80", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }, { baseDuration: 3e4, condition: "is_finalcorner==1&corner==0&distance_diff_top>=5&order_rate>=80", effects: [{ modifier: 2500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "900341": { alternatives: [{ baseDuration: 3e4, condition: "compete_fight_count>0", effects: [{ modifier: 2500, target: 1, type: 27 }], precondition: "distance_rate>=50&order_rate_out40_continue==1" }], rarity: 1 }, "900351": { alternatives: [{ baseDuration: 3e4, condition: "is_finalcorner==1&corner==0&order<=5", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "is_finalcorner==1&blocked_side_continuetime>=2" }], rarity: 1 }, "900361": { alternatives: [{ baseDuration: 3e4, condition: "is_finalcorner==1&corner==0", effects: [{ modifier: 1500, target: 1, type: 27 }, { modifier: 150, target: 1, type: 28 }], precondition: "is_finalcorner==1&order_rate>=40&order_rate<=75&lane_type==0" }], rarity: 1 }, "900371": { alternatives: [{ baseDuration: 3e4, condition: "temptation_count==0&order>=3&is_finalcorner==1&corner==0&order_rate_in80_continue==1&order_rate_out40_continue==1", effects: [{ modifier: 2500, target: 1, type: 27 }], precondition: "" }, { baseDuration: 3e4, condition: "temptation_count==0&order>=3&is_finalcorner==1&corner==0", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "900381": { alternatives: [{ baseDuration: 3e4, condition: "distance_rate>=50&distance_rate<=65&order>=2&order_rate<=40&change_order_onetime<0", effects: [{ modifier: 500, target: 1, type: 27 }, { modifier: 1e3, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "900391": { alternatives: [{ baseDuration: 3e4, condition: "is_finalcorner==1&corner==0&blocked_side_continuetime>=2", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "900401": { alternatives: [{ baseDuration: 36e3, condition: "distance_rate>=50&order_rate>=40&order_rate<=70&is_overtake==1", effects: [{ modifier: 1e3, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "900411": { alternatives: [{ baseDuration: 3e4, condition: "distance_rate>=50&order<=3&blocked_side_continuetime>=2", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "900441": { alternatives: [{ baseDuration: 36e3, condition: "remain_distance<=300", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "order_rate_out50_continue==1&temptation_count==0&is_finalcorner==1" }], rarity: 1 }, "900451": { alternatives: [{ baseDuration: 0, condition: "phase_random==1&order>=2&order_rate<=40", effects: [{ modifier: 350, target: 1, type: 9 }], precondition: "" }], rarity: 1 }, "900461": { alternatives: [{ baseDuration: 3e4, condition: "phase==1&corner==0&order<=2&bashin_diff_behind<=1", effects: [{ modifier: 500, target: 1, type: 27 }, { modifier: 1e3, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "900481": { alternatives: [{ baseDuration: 3e4, condition: "is_finalcorner==1&corner==0&order_rate<=40&overtake_target_time>=1@is_finalcorner==1&corner==0&order_rate<=40&is_overtake==1", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "900501": { alternatives: [{ baseDuration: 3e4, condition: "is_finalcorner==1&order_rate>=40&order_rate<=75&is_overtake==1", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "900511": { alternatives: [{ baseDuration: 24e3, condition: "phase>=2&is_finalcorner_laterhalf==1&order>=3&order_rate<=40@phase>=2&is_finalcorner==1&corner==0&order>=3&order_rate<=40", effects: [{ modifier: 2e3, target: 1, type: 31 }], precondition: "phase==1&blocked_side_continuetime>=2&corner!=0" }], rarity: 1 }, "900521": { alternatives: [{ baseDuration: 0, condition: "is_finalcorner==1&corner!=0&order_rate>50&near_count>=1", effects: [{ modifier: 150, target: 1, type: 9 }], precondition: "" }], rarity: 1 }, "900531": { alternatives: [{ baseDuration: 12e3, condition: "phase>=2&order_rate>=50&is_finalcorner==1&bashin_diff_infront<=1", effects: [{ modifier: 500, target: 1, type: 31 }, { modifier: 500, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "900561": { alternatives: [{ baseDuration: 3e4, condition: "phase>=2&order>=3&blocked_front==1", effects: [{ modifier: 1500, target: 1, type: 27 }, { modifier: 500, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "900581": { alternatives: [{ baseDuration: 3e4, condition: "phase>=2&change_order_onetime<0", effects: [{ modifier: 500, target: 1, type: 27 }, { modifier: 1e3, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "900591": { alternatives: [{ baseDuration: 24e3, condition: "phase>=2&corner!=0&is_finalcorner==0&temptation_count==0&order_rate>=50&order_rate<=70@phase==1&corner!=0&is_finalcorner==1&temptation_count==0&order_rate>=50&order_rate<=70", effects: [{ modifier: 2e3, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "900601": { alternatives: [{ baseDuration: 3e4, condition: "phase>=2&order==3&bashin_diff_behind<=1", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "900611": { alternatives: [{ baseDuration: 3e4, condition: "temptation_count==0&remain_distance<=201&remain_distance>=199&order>=4&order_rate<=70", effects: [{ modifier: 2500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "900621": { alternatives: [{ baseDuration: 3e4, condition: "distance_rate>=50&phase==1&order>=3&order_rate<=70", effects: [{ modifier: 150, target: 1, type: 9 }, { modifier: 500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "900641": { alternatives: [{ baseDuration: 36e3, condition: "distance_rate>=50&order_rate_in20_continue==1", effects: [{ modifier: 150, target: 1, type: 9 }, { modifier: 500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "900671": { alternatives: [{ baseDuration: 3e4, condition: "is_last_straight_onetime==1&order>=2&order<=5&distance_diff_top<=5", effects: [{ modifier: 2500, target: 1, type: 27 }], precondition: "" }, { baseDuration: 3e4, condition: "is_last_straight_onetime==1&order>=2&order<=5&distance_diff_top>5", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "900681": { alternatives: [{ baseDuration: 3e4, condition: "phase==2&straight_front_type==2&order<=2", effects: [{ modifier: 500, target: 1, type: 27 }, { modifier: 1e3, target: 1, type: 31 }], precondition: "" }, { baseDuration: 3e4, condition: "distance_rate>=50&corner==3&order<=2", effects: [{ modifier: 500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "900691": { alternatives: [{ baseDuration: 3e4, condition: "remain_distance<=300&order_rate<=40&bashin_diff_infront<=1", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "900711": { alternatives: [{ baseDuration: 24e3, condition: "is_finalcorner==1&corner==0&bashin_diff_behind<=1", effects: [{ modifier: 2500, target: 1, type: 27 }, { modifier: -100, target: 1, type: 9 }], precondition: "" }], rarity: 1 }, "900721": { alternatives: [{ baseDuration: 3e4, condition: "remain_distance<=300&order_rate<=40", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "is_finalcorner==1&change_order_onetime<0" }], rarity: 1 }, "900741": { alternatives: [{ baseDuration: 3e4, condition: "distance_rate>=50&order_rate>=40&order_rate<=80", effects: [{ modifier: 350, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "910011": { alternatives: [{ baseDuration: 0, condition: "phase==1&order>=2&order_rate<=70&activate_count_middle>=2", effects: [{ modifier: 150, target: 1, type: 9 }], precondition: "" }], rarity: 1 }, "910031": { alternatives: [{ baseDuration: 3e4, condition: "is_finalcorner==1&corner==0", effects: [{ modifier: 2500, target: 1, type: 27 }], precondition: "is_finalcorner==1&is_overtake==1&order<=5&order_rate<=50&overtake_target_no_order_up_time>=2" }], rarity: 1 }, "910041": { alternatives: [{ baseDuration: 3e4, condition: "distance_rate>=50&activate_count_heal>=1&order<=3", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "910051": { alternatives: [{ baseDuration: 24e3, condition: "is_finalcorner==1&change_order_onetime<0&order_rate<=40", effects: [{ modifier: 2500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "910061": { alternatives: [{ baseDuration: 3e4, condition: "activate_count_heal>=3&distance_rate>=50", effects: [{ modifier: 500, target: 1, type: 27 }, { modifier: 1e3, target: 1, type: 31 }, { modifier: 50, target: 1, type: 9 }], precondition: "" }], rarity: 1 }, "910071": { alternatives: [{ baseDuration: 3e4, condition: "distance_rate_after_random==50", effects: [{ modifier: 350, target: 1, type: 27 }, { modifier: 1e4, target: 1, type: 37 }], precondition: "" }], rarity: 1 }, "910101": { alternatives: [{ baseDuration: 3e4, condition: "distance_diff_top<=5&order>=2&order_rate<=40&remain_distance<=201&remain_distance>=199", effects: [{ modifier: 1500, target: 1, type: 27 }, { modifier: 350, target: 1, type: 22 }], precondition: "" }, { baseDuration: 3e4, condition: "order>=2&order_rate<=40&remain_distance<=201&remain_distance>=199", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "910111": { alternatives: [{ baseDuration: 0, condition: "phase==1&change_order_onetime>0&order_rate>=40", effects: [{ modifier: 350, target: 1, type: 9 }], precondition: "" }], rarity: 1 }, "910131": { alternatives: [{ baseDuration: 36e3, condition: "phase>=2&is_finalcorner==1&order<=4&overtake_target_time>=1", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "910141": { alternatives: [{ baseDuration: 24e3, condition: "is_finalcorner==1&corner!=0&is_overtake==1&order>=4&order_rate<=75", effects: [{ modifier: 2e3, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "910151": { alternatives: [{ baseDuration: 3e4, condition: "remain_distance<=401&remain_distance>=399&order_rate<=40&activate_count_all>=7", effects: [{ modifier: 2500, target: 1, type: 27 }], precondition: "" }, { baseDuration: 3e4, condition: "remain_distance<=401&remain_distance>=399&order_rate<=40&activate_count_all<=6", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "910171": { alternatives: [{ baseDuration: 36e3, condition: "is_finalcorner==1", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "phase>=2&order_rate<=50&overtake_target_time>=2" }], rarity: 1 }, "910181": { alternatives: [{ baseDuration: 3e4, condition: "phase==1&order>=3&order_rate<=50&is_overtake==1", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "910201": { alternatives: [{ baseDuration: 3e4, condition: "phase>=2&corner==0&order<=2&straight_front_type==2", effects: [{ modifier: 1500, target: 1, type: 27 }, { modifier: 700, target: 1, type: 31 }], precondition: "" }, { baseDuration: 3e4, condition: "phase>=2&corner==0&order<=2", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "910221": { alternatives: [{ baseDuration: 3e4, condition: "remain_distance>=401&phase>=2&is_finalcorner==1&order_rate>=20&order_rate<=40", effects: [{ modifier: 1500, target: 1, type: 27 }, { modifier: 500, target: 1, type: 31 }], precondition: "" }, { baseDuration: 3e4, condition: "phase>=2&is_finalcorner==1&order_rate>=20&order_rate<=40", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "910231": { alternatives: [{ baseDuration: 3e4, condition: "order_rate_in50_continue==1&phase==1&distance_rate>=50&order>=2&order_rate<=40", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "910241": { alternatives: [{ baseDuration: 3e4, condition: "is_finalcorner==1&order_rate<=40&change_order_onetime<0", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }, { baseDuration: 24e3, condition: "is_finalcorner==1&order_rate>=50&order_rate<=80&change_order_onetime<0", effects: [{ modifier: 2e3, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "910261": { alternatives: [{ baseDuration: 3e4, condition: "order<=4&phase==1&corner!=0&bashin_diff_behind<=3", effects: [{ modifier: 1500, target: 1, type: 27 }, { modifier: 35, target: 1, type: 9 }], precondition: "" }], rarity: 1 }, "910301": { alternatives: [{ baseDuration: 3e4, condition: "phase==1&order>=2&order_rate<=50", effects: [{ modifier: 150, target: 1, type: 9 }, { modifier: -25, target: 9, type: 9 }, { modifier: 500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "910371": { alternatives: [{ baseDuration: 36e3, condition: "change_order_up_finalcorner_after>=2&is_finalcorner==1&corner==0", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "910381": { alternatives: [{ baseDuration: 3e4, condition: "remain_distance<=350&order_rate<=40&order_rate>=20&bashin_diff_behind<=1", effects: [{ modifier: 500, target: 1, type: 27 }, { modifier: 1e3, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "910401": { alternatives: [{ baseDuration: 3e4, condition: "is_finalcorner==1&corner!=0&order_rate>=30&order_rate<=70&blocked_side_continuetime>=2", effects: [{ modifier: 500, target: 1, type: 27 }, { modifier: 1e3, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "910451": { alternatives: [{ baseDuration: 3e4, condition: "is_finalcorner==1&corner==0&order<=4&bashin_diff_infront<=1", effects: [{ modifier: 500, target: 1, type: 27 }, { modifier: 1e3, target: 1, type: 31 }], precondition: "" }], rarity: 1 }, "910521": { alternatives: [{ baseDuration: 3e4, condition: "is_finalcorner==1&corner!=0&distance_diff_top>=7", effects: [{ modifier: 500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "910561": { alternatives: [{ baseDuration: 3e4, condition: "distance_rate>=50&order_rate>=40&overtake_target_time>=2", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "910591": { alternatives: [{ baseDuration: 3e4, condition: "distance_rate>=60&slope==2&phase==1&order_rate>=40&order_rate<=80&remain_distance>=500", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "910601": { alternatives: [{ baseDuration: 36e3, condition: "is_finalcorner==1&corner==0&order_rate>=40&order_rate<=70&popularity>=4", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "is_finalcorner==1&change_order_onetime<0" }, { baseDuration: 3e4, condition: "is_finalcorner==1&corner==0&order_rate>=40&order_rate<=70&popularity<4", effects: [{ modifier: 1500, target: 1, type: 27 }], precondition: "is_finalcorner==1&change_order_onetime<0" }], rarity: 1 }, "910611": { alternatives: [{ baseDuration: 24e3, condition: "is_finalcorner==1&corner==0", effects: [{ modifier: 2e3, target: 1, type: 31 }], precondition: "distance_rate>=50&order_rate_out70_continue==1&temptation_count==0" }], rarity: 1 }, "920011": { alternatives: [{ baseDuration: 3e4, condition: "phase>=2&is_finalcorner==1&corner!=0&is_activate_any_skill==1", effects: [{ modifier: 2500, target: 1, type: 27 }], precondition: "track_id==10005" }, { baseDuration: 3e4, condition: "phase>=2&is_finalcorner==1&corner!=0&is_activate_any_skill==1", effects: [{ modifier: 500, target: 1, type: 27 }], precondition: "" }], rarity: 1 }, "920131": { alternatives: [{ baseDuration: 3e4, condition: "distance_rate>=50&order_rate<=40", effects: [{ modifier: 350, target: 1, type: 27 }], precondition: "" }], rarity: 1 } };

  // vendor/kachi-uma-tools/uma-skill-tools/RaceSolverBuilder.ts
  var GroundSpeedModifier = Object.freeze([
    null,
    // ground types started at 1
    [0, 0, 0, 0, -50],
    [0, 0, 0, 0, -50]
  ].map((o) => Object.freeze(o)));
  var GroundPowerModifier = Object.freeze([
    null,
    [0, 0, -50, -50, -50],
    [0, -100, -50, -100, -100]
  ].map((o) => Object.freeze(o)));
  var StrategyProficiencyModifier = Object.freeze([1.1, 1, 0.85, 0.75, 0.6, 0.4, 0.2, 0.1]);
  var Asitame;
  ((Asitame2) => {
    Asitame2.StrategyDistanceCoefficient = Object.freeze([
      [],
      // distances are 1-indexed (as are strategies, hence the 0 in the first column for every row)
      [0, 1, 0.7, 0.75, 0.7, 1],
      // short (nige, senkou, sasi, oikomi, oonige)
      [0, 1, 0.8, 0.7, 0.75, 1],
      // mile
      [0, 1, 0.9, 0.875, 0.86, 1],
      // medium
      [0, 1, 0.9, 1, 0.9, 1]
      // long
    ]);
    Asitame2.BaseModifier = 875e-5;
    function calcApproximateModifier(power, strategy, distance) {
      return Asitame2.BaseModifier * Math.sqrt(power - 1200) * Asitame2.StrategyDistanceCoefficient[distance][strategy];
    }
    Asitame2.calcApproximateModifier = calcApproximateModifier;
  })(Asitame || (Asitame = {}));
  var StaminaSyoubu;
  ((StaminaSyoubu2) => {
    function distanceFactor(distance) {
      if (distance < 2101)
        return 0;
      else if (distance < 2201)
        return 0.5;
      else if (distance < 2401)
        return 1;
      else if (distance < 2601)
        return 1.2;
      else
        return 1.5;
    }
    StaminaSyoubu2.distanceFactor = distanceFactor;
    function calcApproximateModifier(stamina, distance) {
      const randomFactor = 1;
      return Math.sqrt(stamina - 1200) * 85e-4 * distanceFactor(distance) * randomFactor;
    }
    StaminaSyoubu2.calcApproximateModifier = calcApproximateModifier;
  })(StaminaSyoubu || (StaminaSyoubu = {}));
  function parseStrategy(s) {
    if (typeof s != "string") {
      return s;
    }
    switch (s.toUpperCase()) {
      case "NIGE":
        return 1 /* Nige */;
      case "SENKOU":
        return 2 /* Senkou */;
      case "SASI":
      case "SASHI":
        return 3 /* Sasi */;
      case "OIKOMI":
        return 4 /* Oikomi */;
      case "OONIGE":
        return 5 /* Oonige */;
      default:
        throw new Error("Invalid running strategy.");
    }
  }
  function parseAptitude(a, type) {
    if (typeof a != "string") {
      return a;
    }
    switch (a.toUpperCase()) {
      case "S":
        return 0 /* S */;
      case "A":
        return 1 /* A */;
      case "B":
        return 2 /* B */;
      case "C":
        return 3 /* C */;
      case "D":
        return 4 /* D */;
      case "E":
        return 5 /* E */;
      case "F":
        return 6 /* F */;
      case "G":
        return 7 /* G */;
      default:
        throw new Error("Invalid " + type + " aptitude.");
    }
  }
  function parseGroundCondition(g) {
    if (typeof g != "string") {
      return g;
    }
    switch (g.toUpperCase()) {
      case "GOOD":
        return 1 /* Good */;
      case "YIELDING":
        return 2 /* Yielding */;
      case "SOFT":
        return 3 /* Soft */;
      case "HEAVY":
        return 4 /* Heavy */;
      default:
        throw new Error("Invalid ground condition.");
    }
  }
  function parseWeather(w) {
    if (typeof w != "string") {
      return w;
    }
    switch (w.toUpperCase()) {
      case "SUNNY":
        return 1 /* Sunny */;
      case "CLOUDY":
        return 2 /* Cloudy */;
      case "RAINY":
        return 3 /* Rainy */;
      case "SNOWY":
        return 4 /* Snowy */;
      default:
        throw new Error("Invalid weather.");
    }
  }
  function parseSeason(s) {
    if (typeof s != "string") {
      return s;
    }
    switch (s.toUpperCase()) {
      case "SPRING":
        return 1 /* Spring */;
      case "SUMMER":
        return 2 /* Summer */;
      case "AUTUMN":
        return 3 /* Autumn */;
      case "WINTER":
        return 4 /* Winter */;
      case "SAKURA":
        return 5 /* Sakura */;
      default:
        throw new Error("Invalid season.");
    }
  }
  function parseTime(t) {
    if (typeof t != "string") {
      return t;
    }
    switch (t.toUpperCase()) {
      case "NONE":
      case "NOTIME":
        return 0 /* NoTime */;
      case "MORNING":
        return 1 /* Morning */;
      case "MIDDAY":
        return 2 /* Midday */;
      case "EVENING":
        return 3 /* Evening */;
      case "NIGHT":
        return 4 /* Night */;
      default:
        throw new Error("Invalid race time.");
    }
  }
  function parseGrade(g) {
    if (typeof g != "string") {
      return g;
    }
    switch (g.toUpperCase()) {
      case "G1":
        return 100 /* G1 */;
      case "G2":
        return 200 /* G2 */;
      case "G3":
        return 300 /* G3 */;
      case "OP":
        return 400 /* OP */;
      case "PRE-OP":
      case "PREOP":
        return 700 /* PreOP */;
      case "MAIDEN":
        return 800 /* Maiden */;
      case "DEBUT":
        return 900 /* Debut */;
      case "DAILY":
        return 999 /* Daily */;
      default:
        throw new Error("Invalid race grade.");
    }
  }
  function adjustOvercap(stat) {
    return stat > 1200 ? 1200 + Math.floor((stat - 1200) / 2) : stat;
  }
  function buildBaseStats(horseDesc, mood) {
    const motivCoef = 1 + 0.02 * horseDesc.mood;
    return Object.freeze({
      speed: adjustOvercap(horseDesc.speed) * motivCoef,
      stamina: adjustOvercap(horseDesc.stamina) * motivCoef,
      power: adjustOvercap(horseDesc.power) * motivCoef,
      guts: adjustOvercap(horseDesc.guts) * motivCoef,
      wisdom: adjustOvercap(horseDesc.wisdom) * motivCoef,
      strategy: parseStrategy(horseDesc.strategy),
      distanceAptitude: parseAptitude(horseDesc.distanceAptitude, "distance"),
      surfaceAptitude: parseAptitude(horseDesc.surfaceAptitude, "surface"),
      strategyAptitude: parseAptitude(horseDesc.strategyAptitude, "strategy"),
      rawStamina: horseDesc.stamina * motivCoef,
      rawWisdom: adjustOvercap(horseDesc.wisdom) * motivCoef
    });
  }
  function buildAdjustedStats(baseStats, course, ground) {
    const raceCourseModifier = CourseHelpers.courseSpeedModifier(course, baseStats);
    return Object.freeze({
      speed: Math.max(baseStats.speed * raceCourseModifier + GroundSpeedModifier[course.surface][ground], 1),
      stamina: baseStats.stamina,
      power: Math.max(baseStats.power + GroundPowerModifier[course.surface][ground], 1),
      guts: baseStats.guts,
      wisdom: baseStats.wisdom * StrategyProficiencyModifier[baseStats.strategyAptitude],
      strategy: baseStats.strategy,
      distanceAptitude: baseStats.distanceAptitude,
      surfaceAptitude: baseStats.surfaceAptitude,
      strategyAptitude: baseStats.strategyAptitude,
      rawStamina: baseStats.rawStamina,
      rawWisdom: baseStats.rawWisdom
    });
  }
  function isTarget(self2, targetType) {
    return targetType == 2 /* All */ || self2 == 3 /* Any */ || self2 == 1 /* Self */ == (targetType == 1 /* Self */);
  }
  function buildSkillEffects(skill, perspective) {
    return skill.effects.map((ef) => ({
      type: SkillType.hasOwnProperty(ef.type) && isTarget(perspective, ef.target) ? ef.type : 0 /* Noop */,
      baseDuration: skill.baseDuration / 1e4,
      modifier: ef.modifier / 1e4
    }));
  }
  function buildSkillData(horse, raceParams, course, wholeCourse, parser, skillId, perspective, ignoreNullEffects = false) {
    if (!(skillId in skill_data_default)) {
      throw new Error("bad skill ID " + skillId);
    }
    const extra = Object.assign({ skillId }, raceParams);
    const alternatives = skill_data_default[skillId].alternatives;
    const triggers = [];
    for (let i = 0; i < alternatives.length; ++i) {
      const skill = alternatives[i];
      let full = new RegionList();
      wholeCourse.forEach((r) => full.push(r));
      if (skill.precondition) {
        const pre = parser.parse(parser.tokenize(skill.precondition));
        const preRegions = pre.apply(wholeCourse, course, horse, extra)[0];
        if (preRegions.length == 0) {
          continue;
        } else {
          const bounds = new Region(preRegions[0].start, wholeCourse[wholeCourse.length - 1].end);
          full = full.rmap((r) => r.intersect(bounds));
        }
      }
      const op = parser.parse(parser.tokenize(skill.condition));
      const [regions, extraCondition] = op.apply(full, course, horse, extra);
      if (regions.length == 0) {
        continue;
      }
      if (triggers.length > 0 && !/is_activate_other_skill_detail|is_used_skill_id/.test(skill.condition)) {
        continue;
      }
      const effects2 = buildSkillEffects(skill, perspective);
      if (effects2.length > 0 || ignoreNullEffects) {
        const rarity = skill_data_default[skillId].rarity;
        triggers.push({
          skillId,
          perspective,
          // for some reason 1*/2* uniques, 1*/2* upgraded to 3*, and naturally 3* uniques all have different rarity (3, 4, 5 respectively)
          rarity: rarity >= 3 && rarity <= 5 ? 3 : rarity,
          samplePolicy: op.samplePolicy,
          regions,
          extraCondition,
          effects: effects2
        });
      }
    }
    if (triggers.length > 0)
      return triggers;
    const effects = buildSkillEffects(alternatives[0], perspective);
    if (effects.length == 0 && !ignoreNullEffects) {
      return [];
    } else {
      const rarity = skill_data_default[skillId].rarity;
      const afterEnd = new RegionList();
      afterEnd.push(new Region(9999, 9999));
      return [{
        skillId,
        perspective,
        rarity: rarity >= 3 && rarity <= 5 ? 3 : rarity,
        samplePolicy: ImmediatePolicy,
        regions: afterEnd,
        extraCondition: (_) => false,
        effects
      }];
    }
  }
  var conditionsWithActivateCountsAsRandom = Object.freeze(Object.assign({}, Conditions, {
    activate_count_all: random({
      filterGte(regions, n, course, _1, extra) {
        if (n == 7) {
          const rl = new RegionList();
          regions.forEach((r) => rl.push(new Region(r.start, r.start + 11)));
          return rl;
        }
        const bounds = new Region(Math.min(n / 23 - 0.2, 0.6) * course.distance, Math.min(n / 23 + 0.2, 1) * course.distance);
        return regions.rmap((r) => r.intersect(bounds));
      },
      filterLte(regions, n, course, _1, extra) {
        return new RegionList();
      }
    }),
    activate_count_end_after: random({
      filterGte(regions, _0, course, _1, extra) {
        const bounds = new Region(CourseHelpers.phaseStart(course.distance, 2), CourseHelpers.phaseEnd(course.distance, 3));
        return regions.rmap((r) => r.intersect(bounds));
      }
    }),
    activate_count_heal: noopRandom,
    activate_count_later_half: random({
      filterGte(regions, _0, course, _1, extra) {
        const bounds = new Region(course.distance / 2, course.distance);
        return regions.rmap((r) => r.intersect(bounds));
      }
    }),
    activate_count_middle: random({
      filterGte(regions, n, course, _1, extra) {
        const start = CourseHelpers.phaseStart(course.distance, 1), end = CourseHelpers.phaseEnd(course.distance, 1);
        const bounds = new Region(start, start + n / 10 * (end - start));
        return regions.rmap((r) => r.intersect(bounds));
      }
    }),
    activate_count_start: immediate({
      // for 地固め
      filterGte(regions, _0, course, _1, extra) {
        const bounds = new Region(CourseHelpers.phaseStart(course.distance, 0), CourseHelpers.phaseEnd(course.distance, 0));
        return regions.rmap((r) => r.intersect(bounds));
      }
    })
  }));
  var defaultParser = getParser();
  var acrParser = getParser(conditionsWithActivateCountsAsRandom);
  var RaceSolverBuilder = class _RaceSolverBuilder {
    constructor(nsamples) {
      this.nsamples = nsamples;
      this._course = null;
      this._raceParams = {
        mood: 2,
        groundCondition: 1 /* Good */,
        weather: 1 /* Sunny */,
        season: 1 /* Spring */,
        time: 2 /* Midday */,
        grade: 100 /* G1 */,
        popularity: 1
      };
      this._horse = null;
      this._pacerSkillData = [];
      this._pacerTriggers = [];
      this._pacerSkills = [];
      this._pacerSkillIds = [];
      this._pacerSpeedUpRate = 100;
      this._seed = Math.floor(Math.random() * (-1 >>> 0)) >>> 0;
      this._rng = new Rule30CARng(this._seed);
      this._parser = defaultParser;
      this._skills = [];
      this._samplePolicyOverride = /* @__PURE__ */ new Map();
      this._extraSkillHooks = [];
      this._onSkillActivate = null;
      this._onSkillDeactivate = null;
      this._posKeepMode = 0 /* None */;
      this._mode = void 0;
      this._skillWisdomCheck = void 0;
      this._rushedKakari = void 0;
      this._competeFight = void 0;
      this._leadCompetition = void 0;
      this._laneMovement = void 0;
      this._duelingRates = void 0;
    }
    _course;
    _raceParams;
    _horse;
    _pacerSkills;
    _pacerSkillIds;
    _pacerSpeedUpRate;
    _pacerSkillData;
    _pacerTriggers;
    _rng;
    _seed;
    _parser;
    _skills;
    _samplePolicyOverride;
    _extraSkillHooks;
    _onSkillActivate;
    _onSkillDeactivate;
    _posKeepMode;
    _mode;
    _skillWisdomCheck;
    _rushedKakari;
    _competeFight;
    _leadCompetition;
    _laneMovement;
    _duelingRates;
    seed(seed) {
      this._seed = seed;
      this._rng = new Rule30CARng(seed);
      return this;
    }
    course(course) {
      if (typeof course == "number") {
        this._course = CourseHelpers.getCourse(course);
      } else {
        this._course = course;
      }
      return this;
    }
    mood(mood) {
      this._raceParams.mood = mood;
      return this;
    }
    ground(ground) {
      this._raceParams.groundCondition = parseGroundCondition(ground);
      return this;
    }
    weather(weather) {
      this._raceParams.weather = parseWeather(weather);
      return this;
    }
    season(season) {
      this._raceParams.season = parseSeason(season);
      return this;
    }
    time(time) {
      this._raceParams.time = parseTime(time);
      return this;
    }
    grade(grade) {
      this._raceParams.grade = parseGrade(grade);
      return this;
    }
    popularity(popularity) {
      this._raceParams.popularity = popularity;
      return this;
    }
    order(start, end) {
      this._raceParams.orderRange = [start, end];
      return this;
    }
    numUmas(n) {
      this._raceParams.numUmas = n;
      return this;
    }
    horse(horse) {
      this._horse = horse;
      return this;
    }
    pacerSpeedUpRate(rate) {
      this._pacerSpeedUpRate = rate;
      return this;
    }
    getSamplePolicyKey(skillId, perspective) {
      return `${skillId}:${perspective}`;
    }
    _isNige() {
      if (typeof this._horse.strategy == "string") {
        return this._horse.strategy.toUpperCase() == "NIGE" || this._horse.strategy.toUpperCase() == "OONIGE";
      } else {
        return this._horse.strategy == 1 /* Nige */ || this._horse.strategy == 5 /* Oonige */;
      }
    }
    setupPacer(horse) {
      const pacer = horse;
      const pacerBaseHorse = pacer ? buildBaseStats(pacer, pacer.mood) : null;
      const pacerHorse = pacer ? buildAdjustedStats(pacerBaseHorse, this._course, this._raceParams.groundCondition) : null;
      const wholeCourse = new RegionList();
      wholeCourse.push(new Region(0, this._course.distance));
      Object.freeze(wholeCourse);
      let pacerSkillData = [];
      if (pacerBaseHorse) {
        this._pacerSkillIds = horse.skills;
        const makePacerSkill = buildSkillData.bind(null, pacerBaseHorse, this._raceParams, this._course, wholeCourse, this._parser);
        pacerSkillData = this._pacerSkillIds.flatMap((id) => makePacerSkill(id, 1 /* Self */));
        this._pacerSkillData = pacerSkillData;
      }
      return pacerHorse;
    }
    setupPacerSkillTriggers(pacerRng) {
      const wholeCourse = new RegionList();
      wholeCourse.push(new Region(0, this._course.distance));
      Object.freeze(wholeCourse);
      let pacerTriggers = [];
      if (this._pacerSkillIds.length > 0) {
        pacerTriggers = this._pacerSkillData.map((sd) => {
          const key = sd.perspective != null ? this.getSamplePolicyKey(sd.skillId, sd.perspective) : sd.skillId;
          const sp = this._samplePolicyOverride.get(key) || sd.samplePolicy;
          return sp.sample(sd.regions, this.nsamples, pacerRng);
        });
      }
      this._pacerTriggers = pacerTriggers;
    }
    buildPacer(pacerHorse, i, pacerRng) {
      this.setupPacerSkillTriggers(pacerRng);
      const pacerSkills = this._pacerSkillData.length > 0 ? this._pacerSkillData.map((sd, sdi) => ({
        skillId: sd.skillId,
        perspective: sd.perspective,
        rarity: sd.rarity,
        trigger: this._pacerTriggers[sdi][i % this._pacerTriggers[sdi].length],
        extraCondition: sd.extraCondition,
        effects: sd.effects
      })) : this._pacerSkills;
      return pacerHorse ? new RaceSolver({
        horse: pacerHorse,
        course: this._course,
        hp: NoopHpPolicy,
        skills: pacerSkills,
        rng: pacerRng,
        speedUpProbability: this._pacerSpeedUpRate,
        posKeepMode: this._posKeepMode,
        mode: this._mode,
        isPacer: true,
        competeFight: this._competeFight,
        leadCompetition: this._leadCompetition,
        duelingRates: this._duelingRates,
        laneMovement: this._laneMovement
      }) : null;
    }
    pacer(horse) {
      return this.setupPacer(horse);
    }
    useDefaultPacer(openingLegAccel = false) {
      const pacer = Object.assign({}, this._horse, { strategy: "Nige" });
      if (openingLegAccel) {
        this._pacerSkills = [{
          skillId: "201601",
          perspective: 1 /* Self */,
          rarity: 1 /* White */,
          trigger: new Region(0, 100),
          extraCondition: (_) => true,
          effects: [{ type: 31 /* Accel */, baseDuration: 3, modifier: 0.2 }]
        }, {
          skillId: "200532",
          perspective: 1 /* Self */,
          rarity: 1 /* White */,
          trigger: new Region(0, 100),
          extraCondition: (_) => true,
          effects: [{ type: 31 /* Accel */, baseDuration: 1.2, modifier: 0.2 }]
        }];
      }
      return this.setupPacer(pacer);
    }
    withActivateCountsAsRandom() {
      this._parser = acrParser;
      return this;
    }
    // NB. must be called after horse and mood are set
    withAsiwotameru() {
      const baseDisplayedPower = this._horse.power * (1 + 0.02 * this._raceParams.mood);
      this._extraSkillHooks.push((skilldata, horse, course) => {
        const power = skilldata.reduce((acc, sd) => {
          const powerUp = sd.effects.find((ef) => ef.type == 3 /* PowerUp */);
          if (powerUp && sd.regions.length > 0 && sd.regions[0].start < 9999) {
            return acc + powerUp.modifier;
          } else {
            return acc;
          }
        }, baseDisplayedPower);
        if (power > 1200) {
          const spurtStart = new RegionList();
          spurtStart.push(new Region(CourseHelpers.phaseStart(course.distance, 2), course.distance));
          skilldata.push({
            skillId: "asitame",
            perspective: 1 /* Self */,
            rarity: 1 /* White */,
            regions: spurtStart,
            samplePolicy: ImmediatePolicy,
            extraCondition: (_) => true,
            effects: [{
              type: 31 /* Accel */,
              baseDuration: 3 / (course.distance / 1e3),
              modifier: Asitame.calcApproximateModifier(power, horse.strategy, course.distanceType)
            }]
          });
        }
      });
      return this;
    }
    withStaminaSyoubu() {
      this._extraSkillHooks.push((skilldata, horse, course) => {
        const stamina = skilldata.reduce((acc, sd) => {
          const staminaUp = sd.effects.find((ef) => ef.type == 2 /* StaminaUp */);
          if (staminaUp && sd.regions.length > 0 && sd.regions[0].start < 9999) {
            return acc + staminaUp.modifier;
          } else {
            return acc;
          }
        }, horse.rawStamina);
        if (stamina > 1200) {
          const spurtStart = new RegionList();
          spurtStart.push(new Region(CourseHelpers.phaseStart(course.distance, 2), course.distance));
          skilldata.push({
            skillId: "staminasyoubu",
            perspective: 1 /* Self */,
            rarity: 1 /* White */,
            regions: spurtStart,
            samplePolicy: ImmediatePolicy,
            // TODO do current speed skills count toward reaching max speed or not?
            extraCondition: (s) => s.currentSpeed >= s.lastSpurtSpeed,
            effects: [{
              type: 27 /* TargetSpeed */,
              baseDuration: 9999,
              modifier: StaminaSyoubu.calcApproximateModifier(stamina, course.distance)
            }]
          });
        }
      });
      return this;
    }
    addSkill(skillId, perspective = 1 /* Self */, samplePolicy, originWisdom) {
      this._skills.push({ id: skillId, p: perspective, originWisdom });
      if (samplePolicy != null) {
        this._samplePolicyOverride.set(this.getSamplePolicyKey(skillId, perspective), samplePolicy);
      }
      return this;
    }
    /**
     * Adds a skill that will be forced to activate at a specific distance on the track.
     * This overrides the skill's normal activation conditions and sample policy.
     * @param skillId The skill ID to add
     * @param position The distance (in meters) where the skill should activate
     * @param perspective Whether this skill is for Self or Other (default: Self)
     * @returns this builder for chaining
     */
    addSkillAtPosition(skillId, position, perspective = 1 /* Self */, originWisdom) {
      const { createFixedPositionPolicy: createFixedPositionPolicy2 } = (init_ActivationSamplePolicy(), __toCommonJS(ActivationSamplePolicy_exports));
      return this.addSkill(skillId, perspective, createFixedPositionPolicy2(position), originWisdom);
    }
    posKeepMode(mode) {
      this._posKeepMode = mode;
      return this;
    }
    mode(mode) {
      this._mode = mode;
      return this;
    }
    skillWisdomCheck(enabled) {
      this._skillWisdomCheck = enabled;
      return this;
    }
    rushedKakari(enabled) {
      this._rushedKakari = enabled;
      return this;
    }
    competeFight(enabled) {
      this._competeFight = enabled;
      return this;
    }
    leadCompetition(enabled) {
      this._leadCompetition = enabled;
      return this;
    }
    laneMovement(enabled) {
      this._laneMovement = enabled;
      return this;
    }
    duelingRates(rates) {
      this._duelingRates = rates;
      return this;
    }
    onSkillActivate(cb) {
      this._onSkillActivate = cb;
      return this;
    }
    onSkillDeactivate(cb) {
      this._onSkillDeactivate = cb;
      return this;
    }
    desync() {
      this.seed(this._rng.int32());
    }
    fork() {
      const clone = new _RaceSolverBuilder(this.nsamples);
      clone._course = this._course;
      clone._raceParams = Object.assign({}, this._raceParams);
      clone._horse = this._horse;
      clone._pacerSkills = this._pacerSkills.slice();
      clone._pacerSkillIds = this._pacerSkillIds.slice();
      clone._pacerSpeedUpRate = this._pacerSpeedUpRate;
      clone._pacerSkillData = this._pacerSkillData.slice();
      clone._pacerTriggers = this._pacerTriggers.slice();
      clone.seed(this._seed);
      clone._parser = this._parser;
      clone._skills = this._skills.slice();
      clone._onSkillActivate = this._onSkillActivate;
      clone._onSkillDeactivate = this._onSkillDeactivate;
      clone._posKeepMode = this._posKeepMode;
      clone._mode = this._mode;
      clone._skillWisdomCheck = this._skillWisdomCheck;
      clone._rushedKakari = this._rushedKakari;
      clone._competeFight = this._competeFight;
      clone._leadCompetition = this._leadCompetition;
      clone._laneMovement = this._laneMovement;
      clone._duelingRates = this._duelingRates;
      clone._extraSkillHooks = this._extraSkillHooks.slice();
      return clone;
    }
    *build() {
      let horse = buildBaseStats(this._horse, this._horse.mood);
      let skillRng = new Rule30CARng(this._rng.int32());
      const wholeCourse = new RegionList();
      wholeCourse.push(new Region(0, this._course.distance));
      Object.freeze(wholeCourse);
      const makeSkill = buildSkillData.bind(null, horse, this._raceParams, this._course, wholeCourse, this._parser);
      const skilldata = this._skills.flatMap(({ id, p }) => makeSkill(id, p));
      this._extraSkillHooks.forEach((h) => h(skilldata, horse, this._course));
      const triggers = skilldata.map((sd) => {
        const key = sd.perspective != null ? this.getSamplePolicyKey(sd.skillId, sd.perspective) : sd.skillId;
        const sp = this._samplePolicyOverride.get(key) || sd.samplePolicy;
        return sp.sample(sd.regions, this.nsamples, skillRng);
      });
      horse = buildAdjustedStats(horse, this._course, this._raceParams.groundCondition);
      for (let i = 0; i < this.nsamples; ++i) {
        let solverRng = new Rule30CARng(this._rng.int32());
        const skills = skilldata.map((sd, sdi) => ({
          skillId: sd.skillId,
          perspective: sd.perspective,
          rarity: sd.rarity,
          trigger: triggers[sdi][i % triggers[sdi].length],
          extraCondition: sd.extraCondition,
          effects: sd.effects,
          originWisdom: this._skills.find((input) => input.id === sd.skillId && input.p === sd.perspective)?.originWisdom
        }));
        const hpRng = new Rule30CARng(this._rng.int32());
        const hpPolicy = this._mode === "compare" ? new GameHpPolicy(this._course, this._raceParams.groundCondition, hpRng) : NoopHpPolicy;
        const redo = yield new RaceSolver({
          horse,
          course: this._course,
          skills,
          hp: hpPolicy,
          rng: solverRng,
          onSkillActivate: this._onSkillActivate,
          onSkillDeactivate: this._onSkillDeactivate,
          posKeepMode: this._posKeepMode,
          mode: this._mode,
          skillWisdomCheck: this._skillWisdomCheck,
          rushedKakari: this._rushedKakari,
          competeFight: this._competeFight,
          leadCompetition: this._leadCompetition,
          duelingRates: this._duelingRates,
          laneMovement: this._laneMovement
        });
        if (redo) {
          --i;
        }
      }
    }
  };

  // vendor/kachi-uma-tools/umalator-global/skillnames.json
  var skillnames_default = { "1000011": ["Carnival Bonus"], "1000012": ["Carnival Bonus"], "1000013": ["Carnival Bonus"], "1000014": ["Carnival Bonus"], "100011": ["Shooting Star"], "100021": ["The View from the Lead Is Mine!"], "100031": ["Sky-High Teio Step"], "100041": ["Red Shift/LP1211-M"], "100051": ["Lights of Vaudeville"], "100061": ["Triumphant Pulse"], "100071": ["Anchors Aweigh!"], "100081": ["Cut and Drive!"], "100091": ["Resplendent Red Ace"], "100101": ["Shooting for Victory!"], "100101111": ["Gluttonous Ruler"], "100101211": ["Accelerating to My Dreams!"], "100102111": ["Sunshine 'n' Spirit!"], "100102211": ["Ruler of Summer"], "100103111": ["Overwhelming Advance"], "100103211": ["Gutsiest in Japan"], "100111": ["Where There's a Will, There's a Way"], "100121": ["You and Me! One-on-One!"], "100131": ["The Duty of Dignity Calls"], "100141": ["Victoria por plancha \u2606"], "100151": ["This Dance Is for Vittoria!"], "100161": ["Shadow Break"], "100171": ["Behold Thine Emperor's Divine Might"], "100181": ["Blazing Pride"], "100191": ["OMG! (\uFF9F\u2200\uFF9F)  The Final Sprint! \u2606"], "100201": ["Angling and Scheming"], "100201111": ["Fully Focused"], "100201211": ["Otherworldly Front-Runner"], "100202111": ["Ultramarine Getaway"], "100202211": ["R\xE1faga Azul"], "100211": ["White Lightning Comin' Through!"], "100221": ["Fairy Tale"], "100231": ["\u2234win Q.E.D."], "100241": ["Flashy\u2606Landing"], "100251": ["Chasing After You"], "100261": ["G00 1st. F\u221E;"], "100271": ["Let's Pump Some Iron!"], "100281": ["YUMMY\u2606SPEED!"], "100291": ["Snow Bright, Snow Flight"], "100301": ["Blue Rose Closer"], "100301111": ["Talented Technique"], "100301211": ["Teio Step"], "100302111": ["Racing Prodigy"], "100302211": ["To the Ends of the Earth"], "100311": ["All Charged! It's Go Time!"], "100321": ["U=ma2"], "100331": ["Shooting Star of Dioskouroi"], "100341": ["Now We're Cruisin'!"], "100351": ["Our Ticket to Win!"], "100361": ["trigger:BEAT"], "100371": ["Schwarzes Schwert"], "100381": ["#LookatCurren"], "100391": ["A Princess Must Seize Victory!"], "100401": ["KEEP IT REAL."], "100401111": ["Let's Floor It, Baby!"], "100401211": ["Revved Up Red-Hot"], "100402111": ["You Can't Handle Me \u2606"], "100402211": ["Queen of the Stage"], "100411": ["Genius x Bakushin = Victory"], "100421": ["I'm Possible!"], "100431": ["Ding Dong, Boo!"], "100441": ["Victory belongs to me\u2014Strelitzia! \u2606"], "100451": ["Pure Heart"], "100461": ["SPARKLY\u2606STARDOM"], "100471": ["Raise My Soul's Blade!"], "100481": ["Pop & Polish"], "100491": ["Laugh at the Odds"], "100501": ["Nemesis"], "100501111": ["Dazzling Star"], "100501211": ["Entertainer"], "100502111": ["Stunning Trick"], "100502211": ["Glittering Star"], "100511": ["Budding Blossom"], "100521": ["Super-Duper Climax"], "100531": ["Red-Hot Discipline!"], "100551": ["Magical\u2606Marvelous\u2605World"], "100561": ["I See Victory in My Future!"], "100571": ["Lyrical Journey"], "100581": ["I Never Goof Up!"], "100591": ["Moving Past, and Beyond"], "100601": ["Just a Little Farther!"], "100601111": ["Kasamatsu's Bottomless Pit"], "100601211": ["Cornered by the Beast"], "100602111": ["Full Belly, Full Energy"], "100602211": ["Holiday Miracle"], "100611": ["Prideful King"], "100621": ["Go, Go, Mun!"], "100641": ["Keep Pushing Ahead"], "100651": ["Hands in the Air Like Ya Don't Care!"], "100661": ["Turbo BLAST!"], "100671": ["Eternal Encompassing Shine"], "100681": ["Victory Cheer!"], "100691": ["Ambition to Surpass the Sakura"], "100701": ["Seirios"], "100701111": ["Her Lordship Seeth All"], "100701211": ["It's All Goin' Downhill from Here!"], "100702111": ["Golshi's Out-of-Body Experience"], "100702211": ["Have Some Squid, Kid!"], "10071": ["Warning Shot!"], "100711": ["A Lifelong Dream, A Moment's Flight"], "100721": ["Peerless Dance of Flowering Flames"], "100741": ["Lovely Spring Breeze"], "100761": ["World in Bloom"], "100781": ["Sunny Breeze"], "100801111": ["A Swig of Strength"], "100801211": ["Feet of Fury"], "100802111": ["Cuttin' Through!"], "100802211": ["Kick It into Top Gear!"], "10081": ["Xceleration"], "100831": ["Mission: Triumph"], "100841": ["Sublimated Thunder"], "100851": ["Ever Supreme"], "100871": ["Silent Letter"], "100901111": ["Perfectly Tuned"], "100901211": ["First Place Is MINE!"], "100902111": ["Rough Queen"], "100902211": ["Door to My Dream"], "10091": ["Red Ace"], "100981": ["Luck Runs My Way"], "100991": ["Shine On, Tomakomai! \u2606"], "101001": ["Never Say Never"], "101001111": ["Shootin' Straight Ahead!"], "101001211": ["Frontier Spirit"], "101002111": ["Takin' Y'all on a Wild Ride!"], "101002211": ["May the Best Uma Win!"], "101041": ["Sunrise Banner\u2014Katsuragi Ace!"], "101051": ["Ad Astra"], "101061": ["Bang! Miracle Shot \u2606"], "101101111": ["Yamato Nadeshiko"], "101101211": ["Indomitable Spirit"], "101102111": ["Comforting Heal"], "101102211": ["The Magic of Courage"], "10111": ["Focused Mind"], "101201111": ["Cry of Victory"], "101201211": ["Pororoca Roar"], "101202111": ["Engaging Engine!"], "101202211": ["Cake-Cuttin' Time!"], "101301111": ["As Befits a Mejiro"], "101301211": ["Top-Class Composure"], "101302111": ["Rigorous Rest"], "101302211": ["Above the Cloudline"], "101303111": ["Classy Summer"], "101303211": ["An Earnest Breather"], "101401111": ["Eyes of the Condor"], "101401211": ["Performing Perfecto!"], "101402111": ["Heroic Flurry"], "101402211": ["Rising Condor"], "10141": ["Coraz\xF3n \u2606 Ardiente"], "101501111": ["Entrance of the Centurial Overlord!"], "101501211": ["The Leading Actor"], "101502111": ["Maestro of the Waltz"], "101502211": ["Never a Beat Late!"], "101601111": ["Decisive Strike"], "101601211": ["In the Wolf's Maw"], "101602111": ["Blazing Wolf"], "101602211": ["Ravenous Beast"], "101701111": ["Eyes of an Emperor"], "101701211": ["Unquestioned Might"], "101702111": ["Unwavering Expression"], "101702211": ["Autumn Grace"], "101801111": ["As the Empress Wills"], "101801211": ["Untouchable Beauty"], "101802111": ["Dignified Empress"], "101802211": ["Moonlight Sommelier"], "10181": ["Empress's Pride"], "101901111": ["Ultimate Uma Simp"], "101901211": ["Best View of Anyone!"], "101902111": ["Run Like the Fic's Due Tomorrow!"], "101902211": ["Back for More Moe!"], "102001111": ["Quick Reel"], "102001211": ["Breakaway Scheme"], "102002111": ["Run If You Can \u266A"], "102002211": ["Record Runner"], "102101111": ["Gone in a Flash"], "102101211": ["Paradise Tail"], "102102111": ["A Prayer for Our Predecessors"], "102102211": ["C'mon, Let's Do This!"], "102201111": ["Royal Star"], "102201211": ["Heavenly Descent"], "102202111": ["Emboldening Memories"], "102202211": ["Spear of Morr\xEDgan"], "102301111": ["All According to Plan"], "102301211": ["Strategic Cooldown"], "102302111": ["Very Important Positioning"], "102302211": ["Speedy Santa"], "102401111": ["You copy?"], "102401211": ["I copy!"], "102402111": ["Soft White Whisper"], "102402211": ["Barrel Roll!"], "10241": ["1st Place Kiss\u2606"], "102501111": ["Coffee Break"], "102501211": ["Don't Leave Me..."], "102601111": ["Overclock"], "102601211": ["Overcome Any Strife"], "102602111": ["Malfunction Mitigation"], "102602211": ["Perfect Boost"], "102701111": ["Let Those Muscles SING!"], "102701211": ["Bulk Up & Away!"], "102702111": ["Helping Grant Your Dream"], "102702211": ["Give You a Taste of Glory"], "10271": ["Feel the Burn!"], "102801111": ["Full Tank!"], "102801211": ["Always Hungry for More!"], "102901111": ["Like They Do in the City!"], "102901211": ["Snow Gals Know Howta Sled!"], "103001111": ["Heroic Determination"], "103001211": ["Dark Assassin"], "103002111": ["I Can Do This!"], "103002211": ["Trick and Treat"], "103101111": ["Stubborn Sis"], "103101211": ["Wind God's Wrath"], "103102111": ["Perfect \u266A Order Up!"], "103102211": ["Slick Serving"], "103201111": ["Latent One"], "103201211": ["Efficient Rest Methodology"], "103202111": ["Devil's Laplace"], "103202211": ["Blinding Luxon"], "10321": ["Introduction to Physiology"], "103301111": ["Quiet Promise"], "103301211": ["Self-Destructive Comet"], "103401111": ["Firefighter's Ascent"], "103401211": ["Expert on the Dirt"], "103402111": ["Fan Dance"], "103402211": ["Standout Step"], "103501111": ["Never Ever Give Up"], "103501211": ["Winning Road"], "103502111": ["Superpowered Engines!"], "103502211": ["I Can Fly!"], "10351": ["V Is for Victory!"], "103601111": ["Uplift"], "103601211": ["Lose Myself"], "103701111": ["Blinding Magia"], "103701211": ["On My Honor"], "103702111": ["Recipe for Victory"], "103702211": ["Dark Flash"], "103801111": ["#CuteChaos\u266A"], "103801211": ["#GotYouDownBad\u266A"], "103802111": ["#ThinkOfMe"], "103802211": ["#BeautifulSprint"], "103901111": ["Unstoppable Princess"], "103901211": ["Rising Road Rage"], "104001111": ["Pristine Foresight"], "104001211": ["Star of the Runway"], "104002111": ["Golden Formation"], "104002211": ["Geared to Strike"], "104101111": ["Bakushin SPRINT!"], "104101211": ["Bakushin LEAD!"], "104102111": ["Full Charge! BAKUSHIIIN!"], "104102211": ["Baku-speed! BakuSHIIIIN!"], "10411": ["Class Rep + Speed = Bakushin"], "104201111": ["The World Is My Oyster"], "104201211": ["Pearlescent Danceoff"], "104301111": ["Just Run Like Crazy!"], "104301211": ["Mischevious Maestro"], "104401111": ["After-School Magical Girl"], "104401211": ["Magic Charm"], "104501111": ["Sweet Trance"], "104501211": ["Motherly Maestro"], "104502111": ["Expert Caretaker"], "104502211": ["Mummy's Sweet Whispers"], "10451": ["Clear Heart"], "104601111": ["Umadol\u2605Onstage!"], "104601211": ["Umadol \u266A Chart Topper!"], "104602111": ["Rising to Umadol Stardom!"], "104602211": ["Always Center Stage!"], "104701111": ["Halfway to Heroism"], "104701211": ["Shining Sword"], "104801111": ["Lezzgo!"], "104801211": ["Trendsetter"], "104802111": ["I Can Totes Shake 'Em!"], "104802211": ["I'm Literally the Best!"], "104901111": ["It's All or Nothing"], "104901211": ["Takin' Each Gamble"], "105001111": ["Cutting-Edge Senses"], "105001211": ["Heart of a Hunter"], "105002111": ["I'll Show You!"], "105002211": ["Bolt from the Brink"], "105101111": ["Petals in the Breeze"], "105101211": ["Blooming Resolve"], "105102111": ["Flower Girl's Blessing"], "105102211": ["Veil in the Spring Breeze"], "105201111": ["I'll Keep Trying!"], "105201211": ["I Won't Lose!"], "105202111": ["Look, Look!"], "105202211": ["I'll Do My Very Best!"], "10521": ["Super-Duper Stoked"], "105301111": ["Peak Discipline"], "105301211": ["Ready for a Red-Hot Tech!"], "105302111": ["Red-Hot Transforma-Runner!"], "105302211": ["Ultimate Ultra-Sprint!"], "105501111": ["Marvelous \u2606 Thrill"], "105501211": ["Make the World Marvelous \u2606"], "105601111": ["Lucky Sign!"], "105601211": ["Lucky Number Seven"], "105602111": ["Crazy Extra Mega Lucky!"], "105602211": ["Bring On the Luck!"], "10561": ["Luck Be with Me!"], "105701111": ["Soaring Steps"], "105701211": ["Spring from the Earth"], "105801111": ["Right on the Dot"], "105801211": ["I-I'm Doing My Best!"], "105802111": ["For My Rival!"], "105802211": ["Soul Guide"], "105901111": ["Cold Look"], "105901211": ["Queen of Yodo"], "105902111": ["I Want to Explore More!"], "105902211": ["First Step from the Brink"], "106001111": ["Cynical Charm"], "106001211": ["Be the Lead!"], "106002111": ["Just Gotta Do What I Can"], "106002211": ["Keep the Cheers Comin'"], "10601": ["I Can Win Sometimes, Right?"], "106101111": ["Shocking Sweep"], "106101211": ["Kingly View"], "106102111": ["Ascend Glory"], "106102211": ["Sakura Blossoms in the Breeze"], "10611": ["Call Me King"], "106201111": ["Goin' My Way!"], "106201211": ["Ei, Ei, Mun!"], "106202111": ["Positive Smile"], "106202211": ["Turning Things Right Around!"], "10621": ["Ready, Go!"], "106401111": ["WILD, Amirite?"], "106401211": ["Breakaway on Lock"], "106501111": ["LOL I'm on FIRE!"], "106501211": ["DW I'm on It!"], "106601111": ["1000% Output!"], "106601211": ["Turbo's Leadin' the Charge!"], "10661": ["Engines LIT!"], "106701111": ["Indestructable"], "106701211": ["Rondo to the Right"], "106702111": ["Beyond My Station"], "106702211": ["The Dawn Always Comes!"], "106801111": ["Get the Festival Going!"], "106801211": ["Bright Blast"], "106802111": ["Leap Ahead to the Future"], "106802211": ["Introducing the Victor!"], "106901111": ["Good Year for Sakura!"], "106901211": ["The Sakura Bloom Regally Today"], "106902111": ["Bloom Down the Branch"], "106902211": ["I Don't Wanna Lose!"], "107001111": ["Unyielding Aster"], "107001211": ["Arabesque of the Brightest Star"], "107101111": ["Eternal Light"], "107101211": ["Give This Moment Everything"], "107102111": ["Elegant Phosphorescence"], "107102211": ["Fleeting Afterglow"], "107201111": ["Growing Flame"], "107201211": ["Yaeno's Fighting Spirit"], "107401111": ["Ladylike Resolve"], "107401211": ["Immovable as a Mountain"], "107601111": ["Open All the Buds"], "107601211": ["Blossom for Us!"], "107801111": ["Shearing Gust"], "107801211": ["As Nature Wills"], "108301111": ["Simple and Sturdy"], "108301211": ["Imposing Innovation"], "108401111": ["The Stormcaller of Destruction"], "108401211": ["Intoxicate the World"], "108501111": ["Ever Graceful"], "108501211": ["Always the Most Brilliant"], "108701111": ["Raise the Pitch"], "108701211": ["Leave a Footprint in Your Memory"], "109801111": ["Lucky Rickey!"], "109801211": ["Longmai Pulse"], "109901111": ["Thinkin' About Tomakomai Food"], "109901211": ["Be a Good Ambassador!"], "1100011": ["Feelin' a Bit Silly"], "110001111": ["A Dose of Ambition"], "110001211": ["Divine on the Dirt"], "110011": ["Dazzl'n \u266A Diver"], "110021": ["Ahead of the Horizon"], "110031": ["Certain Victory"], "110041": ["A Kiss for Courage"], "110051": ["Ravissant"], "110061": ["Festive Miracle"], "110071": ["564 Escapades"], "110081": ["Into High Gear!"], "110091": ["Queen's Lumination"], "110101": ["Joyful Voyage!"], "110111": ["Superior Heal"], "110121": ["First Bite of the Feast!"], "110131": ["Legacy of the Strong"], "110141": ["Condor's Fury"], "110151": ["Barcarole of Blessings"], "110161": ["Free From the Ashes"], "110171": ["Arrows Whistle, Shadows Disperse"], "110181": ["Eternal Moments"], "110191": ["THE MOE AAAA Thanks for My Life"], "110201": ["Break It Down!"], "110211": ["Lightning Flare"], "110221": ["Best Day Ever"], "110231": ["Presents from X"], "110241": ["Flowery\u2606Maneuver"], "110261": ["Operation Cacao"], "110271": ["A Warm Cup for You"], "110301": ["Every Rose Has Its Fangs"], "110311": ["Fresh\u2606Parlor"], "110321": ["Summer Halation"], "110341": ["Firelight"], "110351": ["Ticket to Your Dreams!"], "110371": ["Guten Appetit \u266A"], "110381": ["One True Color"], "110401": ["Dancing in the Leaves"], "110401111": ["Aced It!"], "110401211": ["Runnin' the Race Solo"], "110411": ["Cherry\u2606Scramble"], "110451": ["Give Mummy a Hug \u2661"], "110461": ["\u03B1-star*"], "110481": ["Us Girlies Keep Winnin'! \u266A"], "110501": ["Hephaestus"], "110501111": ["Beyond the Stars with You"], "110501211": ["Cross the Sea of Stars"], "110511": ["Flowering Dreams"], "110521": ["114th Time's the Charm"], "110531": ["Scorching Summer Tech!"], "110561": ["Bountiful Harvest"], "110581": ["Spooky, Scary, Happy"], "110591": ["Wherever This Wonder Leads"], "110601": ["Go\u2606Go\u2606Goal!"], "110601111": ["No Pressure, No Rush"], "110601211": ["Collect All the Little Miracles"], "110611": ["Louder! Tracen Cheer!"], "110621": ["Tumbly Power Drive!"], "110671": ["Rain Cloud Bolt"], "110681": ["Bring on the Banquet!"], "110691": ["Watch! Me! BLOOM!"], "110711": ["Danser le Pr\xE9sent"], "1200011": ["Medium Race Enthusiast"], "1200021": ["Long Race Enthusiast"], "1200031": ["Mile Race Enthusiast"], "1200041": ["Sprint Race Enthusiast"], "1200051": ["Dirt Race Enthusiast"], "120011": ["Dreams Donned with Pride!"], "120131": ["Your Smile Sparkles as the Waves"], "1300011": ["Energetic Hero"], "1300021": ["Sunny Hero"], "1300031": ["Rainy Hero"], "1300041": ["Inner Hero"], "1300051": ["Outer Hero"], "1300061": ["Friendly Hero"], "1300071": ["Dashing Hero"], "200011": ["Right-Handed \u25CE"], "200012": ["Right-Handed \u25CB"], "200013": ["Right-Handed \xD7"], "200014": ["Right-Handed Demon"], "200021": ["Left-Handed \u25CE"], "200022": ["Left-Handed \u25CB"], "200023": ["Left-Handed \xD7"], "200031": ["Tokyo Racecourse \u25CE"], "200032": ["Tokyo Racecourse \u25CB"], "200033": ["Tokyo Racecourse \xD7"], "200041": ["Nakayama Racecourse \u25CE"], "200042": ["Nakayama Racecourse \u25CB"], "200043": ["Nakayama Racecourse \xD7"], "200051": ["Hanshin Racecourse \u25CE"], "200052": ["Hanshin Racecourse \u25CB"], "200053": ["Hanshin Racecourse \xD7"], "200061": ["Kyoto Racecourse \u25CE"], "200062": ["Kyoto Racecourse \u25CB"], "200063": ["Kyoto Racecourse \xD7"], "200064": ["Yodo Invicta"], "200071": ["Chukyo Racecourse \u25CE"], "200072": ["Chukyo Racecourse \u25CB"], "200073": ["Chukyo Racecourse \xD7"], "200081": ["Sapporo Racecourse \u25CE"], "200082": ["Sapporo Racecourse \u25CB"], "200083": ["Sapporo Racecourse \xD7"], "200091": ["Hakodate Racecourse \u25CE"], "200092": ["Hakodate Racecourse \u25CB"], "200093": ["Hakodate Racecourse \xD7"], "200101": ["Fukushima Racecourse \u25CE"], "200102": ["Fukushima Racecourse \u25CB"], "200103": ["Fukushima Racecourse \xD7"], "200111": ["Niigata Racecourse \u25CE"], "200112": ["Niigata Racecourse \u25CB"], "200113": ["Niigata Racecourse \xD7"], "200121": ["Kokura Racecourse \u25CE"], "200122": ["Kokura Racecourse \u25CB"], "200123": ["Kokura Racecourse \xD7"], "200131": ["Standard Distance \u25CE"], "200132": ["Standard Distance \u25CB"], "200133": ["Standard Distance \xD7"], "200141": ["Non-Standard Distance \u25CE"], "200142": ["Non-Standard Distance \u25CB"], "200143": ["Non-Standard Distance \xD7"], "200151": ["Firm Conditions \u25CE"], "200152": ["Firm Conditions \u25CB"], "200153": ["Firm Conditions \xD7"], "200154": ["Firm Course Menace"], "200161": ["Wet Conditions \u25CE"], "200162": ["Wet Conditions \u25CB"], "200163": ["Wet Conditions \xD7"], "200171": ["Spring Runner \u25CE"], "200172": ["Spring Runner \u25CB"], "200173": ["Spring Runner \xD7"], "200174": ["Spring Spectacle"], "200181": ["Summer Runner \u25CE"], "200182": ["Summer Runner \u25CB"], "200183": ["Summer Runner \xD7"], "200184": ["Summer Gust"], "200191": ["Fall Runner \u25CE"], "200192": ["Fall Runner \u25CB"], "200193": ["Fall Runner \xD7"], "200194": ["Fall Frenzy"], "200201": ["Winter Runner \u25CE"], "200202": ["Winter Runner \u25CB"], "200203": ["Winter Runner \xD7"], "200211": ["Sunny Days \u25CE"], "200212": ["Sunny Days \u25CB"], "200221": ["Cloudy Days \u25CE"], "200222": ["Cloudy Days \u25CB"], "200231": ["Rainy Days \u25CE"], "200232": ["Rainy Days \u25CB"], "200233": ["Rainy Days \xD7"], "200241": ["Snowy Days \u25CE"], "200242": ["Snowy Days \u25CB"], "200251": ["Inner Post Proficiency \u25CE"], "200252": ["Inner Post Proficiency \u25CB"], "200253": ["Inner Post Averseness"], "200261": ["Outer Post Proficiency \u25CE"], "200262": ["Outer Post Proficiency \u25CB"], "200263": ["Outer Post Averseness"], "200271": ["Maverick \u25CE"], "200272": ["Maverick \u25CB"], "200281": ["Competitive Spirit \u25CE"], "200282": ["Competitive Spirit \u25CB"], "200283": ["Wallflower"], "200291": ["Target in Sight \u25CE"], "200292": ["Target in Sight \u25CB"], "200301": ["Long Shot \u25CE"], "200302": ["Long Shot \u25CB"], "200311": ["G1 Averseness"], "200321": ["Paddock Fright"], "200331": ["Professor of Curvature"], "200332": ["Corner Adept \u25CB"], "200333": ["Corner Adept \xD7"], "200341": ["Corner Connoisseur"], "200342": ["Corner Acceleration \u25CB"], "200343": ["Corner Acceleration \xD7"], "200351": ["Swinging Maestro"], "200352": ["Corner Recovery \u25CB"], "200353": ["Corner Recovery \xD7"], "200361": ["Beeline Burst"], "200362": ["Straightaway Adept"], "200371": ["Rushing Gale!"], "200372": ["Straightaway Acceleration"], "200381": ["Breath of Fresh Air"], "200382": ["Straightaway Recovery"], "200391": ["Ramp Revulsion"], "200401": ["Packphobia"], "200411": ["Defeatist"], "200421": ["Reckless"], "200431": ["Concentration"], "200432": ["Focus"], "200433": ["Gatekept"], "200441": ["Iron Will"], "200442": ["Lay Low"], "200451": ["Center Stage"], "200452": ["Prudent Positioning"], "200461": ["It's On!"], "200462": ["Ramp Up"], "200471": ["Indomitable"], "200472": ["Pace Strategy"], "200481": ["Unruffled"], "200482": ["Calm in a Crowd"], "200491": ["No Stopping Me!"], "200492": ["Nimble Navigator"], "200501": ["Lane Legerdemain"], "200502": ["Go with the Flow"], "200511": ["In Body and Mind"], "200512": ["Homestretch Haste"], "200521": ["Running Idle"], "200531": ["Taking the Lead"], "200532": ["Early Lead"], "200541": ["Escape Artist"], "200542": ["Fast-Paced"], "200551": ["Unrestrained"], "200552": ["Final Push"], "200561": ["Calm and Collected"], "200562": ["Stamina to Spare"], "200571": ["Race Planner"], "200572": ["Preferred Position"], "200581": ["Speed Star"], "200582": ["Prepared to Pass"], "200591": ["Fast & Furious"], "200592": ["Position Pilfer"], "200601": ["On Your Left!"], "200602": ["Slick Surge"], "200611": ["Rising Dragon"], "200612": ["Outer Swell"], "200621": ["Sleeping Lion"], "200622": ["Standing By"], "200631": ["Sturm und Drang"], "200632": ["Masterful Gambit"], "200641": ["Encroaching Shadow"], "200642": ["Straightaway Spurt"], "200651": ["Turbo Sprint"], "200652": ["Sprinting Gear"], "200662": ["Wait-and-See"], "200671": ["Blinding Flash"], "200672": ["Gap Closer"], "200681": ["Mile Maven"], "200682": ["Productive Plan"], "200691": ["Keen Eye"], "200692": ["Watchful Eye"], "200701": ["Furious Feat"], "200702": ["Updrafters"], "200711": ["Trackblazer"], "200712": ["Rosy Outlook"], "200721": ["Killer Tunes"], "200722": ["Up-Tempo"], "200731": ["Unyielding"], "200732": ["Steadfast"], "200741": ["Cooldown"], "200742": ["Deep Breaths"], "200751": ["Innate Experience"], "200752": ["Inside Scoop"], "200761": ["Adrenaline Rush"], "200762": ["Extra Tank"], "200771": ["Trick (Front)"], "200772": ["Tantalizing Trick"], "200781": ["Trick (Rear)"], "200791": ["Frenzied Front Runners"], "200801": ["Frenzied Pace Chasers"], "200811": ["Frenzied Late Surgers"], "200821": ["Frenzied End Closers"], "200831": ["Subdued Front Runners"], "200841": ["Flustered Front Runners"], "200851": ["Hesitant Front Runners"], "200861": ["Subdued Pace Chasers"], "200871": ["Flustered Pace Chasers"], "200881": ["Hesitant Pace Chasers"], "200891": ["Subdued Late Surgers"], "200901": ["Flustered Late Surgers"], "200911": ["Hesitant Late Surgers"], "200921": ["Subdued End Closers"], "200931": ["Flustered End Closers"], "200941": ["Hesitant End Closers"], "200951": ["Oi Racecourse \u25CE"], "200952": ["Oi Racecourse \u25CB"], "200953": ["Oi Racecourse \xD7"], "200961": ["Sprint Straightaways \u25CE"], "200962": ["Sprint Straightaways \u25CB"], "200963": ["Shocking Flash"], "200971": ["Sprint Corners \u25CE"], "200972": ["Sprint Corners \u25CB"], "200981": ["Staggering Lead"], "200982": ["Huge Lead"], "200991": ["Plan X"], "200992": ["Countermeasure"], "201001": ["Perfect Prep!"], "201002": ["Meticulous Measures"], "201011": ["Adored by All"], "201012": ["Intimidate"], "201021": ["You've Got No Shot"], "201022": ["Stop Right There!"], "201031": ["Mile Straightaways \u25CE"], "201032": ["Mile Straightaways \u25CB"], "201041": ["Mile Corners \u25CE"], "201042": ["Mile Corners \u25CB"], "201043": ["Crescent Gust"], "201051": ["Changing Gears"], "201052": ["Shifting Gears"], "201061": ["Step on the Gas!"], "201062": ["Acceleration"], "201071": ["Big-Sisterly"], "201072": ["Unyielding Spirit"], "201081": ["Greed for Speed"], "201082": ["Speed Eater"], "201091": ["Battle Formation"], "201092": ["Opening Gambit"], "201101": ["Medium Straightaways \u25CE"], "201102": ["Medium Straightaways \u25CB"], "201103": ["Flash Forward"], "201111": ["Medium Corners \u25CE"], "201112": ["Medium Corners \u25CB"], "201113": ["Refraction Arc"], "201121": ["Clairvoyance"], "201122": ["Hawkeye"], "201131": ["Lightning Step"], "201132": ["Thunderbolt Step"], "201141": ["Miraculous Step"], "201142": ["Soft Step"], "201151": ["Dominator"], "201152": ["Tether"], "201161": ["Mystifying Murmur"], "201162": ["Murmur"], "201171": ["Long Straightaways \u25CE"], "201172": ["Long Straightaways \u25CB"], "201173": ["Blast Forward"], "201181": ["Long Corners \u25CE"], "201182": ["Long Corners \u25CB"], "201191": ["Vanguard Spirit"], "201192": ["Keeping the Lead"], "201201": ["VIP Pass"], "201202": ["Passing Pro"], "201211": ["Overwhelming Pressure"], "201212": ["Pressure"], "201221": ["Stamina Siphon"], "201222": ["Stamina Eater"], "201231": ["Illusionist"], "201232": ["Smoke Screen"], "201241": ["Front Runner Straightaways \u25CE"], "201242": ["Front Runner Straightaways \u25CB"], "201251": ["Front Runner Corners \u25CE"], "201252": ["Front Runner Corners \u25CB"], "201253": ["Crescent Gale"], "201261": ["Sixth Sense"], "201262": ["Dodging Danger"], "201271": ["Top Runner"], "201272": ["Leader's Pride"], "201281": ["Restless"], "201282": ["Moxie"], "201291": ["Reignition"], "201292": ["Second Wind"], "201302": ["Restart"], "201311": ["Pace Chaser Straightaways \u25CE"], "201312": ["Pace Chaser Straightaways \u25CB"], "201321": ["Pace Chaser Corners \u25CE"], "201322": ["Pace Chaser Corners \u25CB"], "201331": ["Technician"], "201332": ["Shrewd Step"], "201341": ["Determined Descent"], "201342": ["Straight Descent"], "201351": ["Gourmand"], "201352": ["Hydrate"], "201361": ["Shatterproof"], "201362": ["Tactical Tweak"], "201371": ["Dazzling Disorientation"], "201372": ["Disorient"], "201381": ["Late Surger Straightaways \u25CE"], "201382": ["Late Surger Straightaways \u25CB"], "201383": ["Sharp Streak"], "201391": ["Late Surger Corners \u25CE"], "201392": ["Late Surger Corners \u25CB"], "201393": ["Sharp Arc"], "201401": ["Hard Worker"], "201402": ["Fighter"], "201411": ["15,000,000 CC"], "201412": ["1,500,000 CC"], "201421": ["Relax"], "201422": ["A Small Breather"], "201431": ["The Bigger Picture"], "201432": ["Studious"], "201441": ["All-Seeing Eyes"], "201442": ["Sharp Gaze"], "201451": ["End Closer Straightaways \u25CE"], "201452": ["End Closer Straightaways \u25CB"], "201453": ["Moonlit Flash"], "201461": ["End Closer Corners \u25CE"], "201462": ["End Closer Corners \u25CB"], "201471": ["The Coast Is Clear!"], "201472": ["I Can See Right Through You"], "201481": ["Go-Home Specialist"], "201482": ["After-School Stroll"], "201491": ["Serenity"], "201492": ["Levelheaded"], "201501": ["Crusader"], "201502": ["Strategist"], "201511": ["Petrifying Gaze"], "201512": ["Intense Gaze"], "201521": ["Front Runner Savvy \u25CE"], "201522": ["Front Runner Savvy \u25CB"], "201531": ["Pace Chaser Savvy \u25CE"], "201532": ["Pace Chaser Savvy \u25CB"], "201541": ["Late Surger Savvy \u25CE"], "201542": ["Late Surger Savvy \u25CB"], "201551": ["End Closer Savvy \u25CE"], "201552": ["End Closer Savvy \u25CB"], "201561": ["Super Lucky Seven"], "201562": ["Lucky Seven"], "201571": ["Triple 7s"], "201581": ["Highlander"], "201591": ["Uma Stan"], "201592": ["Superstan"], "201601": ["Groundwork"], "201602": ["Solid Preparation"], "201611": ["Tail Held High"], "201612": ["Tail Nine"], "201621": ["Shake It Out"], "201631": ["Sympathy"], "201641": ["Lone Wolf"], "201651": ["Slipstream"], "201652": ["Perfect Spot!"], "201661": ["Playtime's Over!"], "201662": ["See Ya Later!"], "201671": ["Trending in the Charts!"], "201672": ["Top Pick"], "201681": ["Lead the Charge!"], "201682": ["Forward, March!"], "201691": ["Lie in Wait"], "201692": ["Be Still"], "201701": ["Come What May"], "201702": ["All I've Got"], "201801": ["\u2661 3D Nail Art"], "201901": ["Neck and Neck"], "201902": ["Head-On"], "202001": ["Master of the Sands"], "202002": ["Familiar Ground"], "202011": ["Headliner"], "202012": ["Feature Act"], "202021": ["Daring Strike"], "202022": ["Early Start"], "202031": ["Nothing Ventured"], "202032": ["Risky Business"], "202041": ["In High Spirits"], "202042": ["Light as a Feather"], "202051": ["Runaway"], "202061": ["Best in Japan"], "202071": ["Of Calm Mind"], "202072": ["Free-Spirited"], "202081": ["From the Brink"], "202082": ["Take the Chance"], "202091": ["Burning Soul"], "202092": ["Fighting Spirit"], "202101": ["Elated"], "202102": ["Eager"], "202111": ["Full of Vigor"], "202112": ["Pumped"], "202121": ["Dauntless"], "202122": ["Fearless"], "202131": ["Wild Wind"], "202132": ["With All My Soul"], "202141": ["You're Not the Boss of Me!"], "202151": ["Keep Going!"], "202152": ["Full Throttle"], "202161": ["Restraint"], "202171": ["Master of Declines"], "202172": ["Downhill Speedster"], "202181": ["99 Problems"], "202191": ["Heart All Set"], "202192": ["All Set"], "202201": ["Kawasaki Racecourse \u25CE"], "202202": ["Kawasaki Racecourse \u25CB"], "202203": ["Kawasaki Racecourse \xD7"], "202211": ["Funabashi Racecourse \u25CE"], "202212": ["Funabashi Racecourse \u25CB"], "202213": ["Funabashi Racecourse \xD7"], "202221": ["Morioka Racecourse \u25CE"], "202222": ["Morioka Racecourse \u25CB"], "202223": ["Morioka Racecourse \xD7"], "202231": ["Night Races \u25CE"], "202232": ["Night Races \u25CB"], "202233": ["Night Races \xD7"], "202241": ["Sharp Turns \u25CE"], "202242": ["Sharp Turns \u25CB"], "202243": ["Sharp Turns \xD7"], "202251": ["Collaborative Graded Races \u25CE"], "202252": ["Collaborative Graded Races \u25CB"], "202253": ["Collaborative Graded Races \xD7"], "202261": ["Chance of Victory"], "202262": ["Sunny Sign"], "202271": ["Can't Keep Me Down"], "202272": ["Comeback"], "202281": ["Full Speed!"], "202282": ["Full Tilt"], "202291": ["Crystal Clear"], "202292": ["Rational"], "202301": ["Dancer in the Dirt"], "202302": ["Down in the Dirt \u25CB"], "202303": ["Down in the Dirt \xD7"], "202311": ["Be the Center!"], "202312": ["Got the Spirit!"], "202321": ["Run Like Crazy!"], "202322": ["Rapid Gain"], "202331": ["Strong Steps"], "202332": ["Solid Steps"], "202341": ["Maestro of the Mud"], "202342": ["Muddy \u25CE"], "202343": ["Muddy \u25CB"], "202344": ["Muddy \xD7"], "202351": ["Dust Cloud Idol"], "202352": ["Dust Cloud"], "202361": ["Catch 'Em Off Guard"], "202362": ["Oppression"], "202371": ["Unstoppable"], "202372": ["On the Attack"], "202381": ["Breakin' Ahead"], "202382": ["Breakin' Out"], "202391": ["Givin' It 1000%"], "202392": ["Mad Dash"], "202401": ["Flash Forward"], "202402": ["Leap Forward"], "202411": ["Ambitious Breeze"], "202412": ["Aspire"], "202421": ["Claw Forward"], "202422": ["Scramble"], "202431": ["Solid Strike"], "202432": ["Steady Gait"], "202441": ["Risk-Maker"], "202442": ["Risk-Taker"], "202451": ["Top Gear"], "202452": ["Pedal to the Metal"], "202461": ["Can't Even Catch My Shadow"], "202462": ["Firm Resolve"], "202471": ["Hot Pursuit"], "202472": ["Latch On"], "202481": ["Beast Mode"], "202482": ["My True Strength"], "202491": ["Razor-Sharp Steps"], "202492": ["Sharp Steps"], "202501": ["Reckless Charge"], "202502": ["No Looking Back"], "202511": ["Gonna Be Ur #1 FRFR"], "202521": ["Beautiful Form"], "202522": ["Break the Mold"], "202531": ["Superb Gait"], "202532": ["Graceful Gait"], "202541": ["Vying for the Throne"], "202542": ["Pressure"], "202551": ["Divine Speed"], "202552": ["High Speed"], "202561": ["Sure Shot"], "202562": ["Locked On"], "202571": ["Miracle for My Future"], "202581": ["Light-Footed as a Hare"], "202582": ["Undistracted"], "202591": ["In One Fell Swoop"], "202592": ["Hurry Up!"], "202601": ["Luminescence"], "202602": ["Ignition"], "202612": ["Unrestrained Passion"], "202621": ["Rampaging Passion"], "202622": ["Start the Charge"], "202641": ["A Journey in a Step"], "202642": ["First Step"], "202651": ["Part the Clouds"], "202652": ["Confident"], "202661": ["Rising Pulse"], "202662": ["Rising Heat"], "202671": ["High Voltage"], "202672": ["Beating Heart"], "202681": ["Send It! \u2606"], "202682": ["Pop Off!"], "202691": ["Before Anyone!"], "202692": ["Goin' First"], "202701": ["Singularity"], "202702": ["Adventurer"], "210011": ["Burning Spirit SPD"], "210012": ["Ignited Spirit SPD"], "210021": ["Burning Spirit STA"], "210022": ["Ignited Spirit STA"], "210031": ["Burning Spirit PWR"], "210032": ["Ignited Spirit PWR"], "210041": ["Burning Spirit GUTS"], "210042": ["Ignited Spirit GUTS"], "210051": ["Burning Spirit WIT"], "210052": ["Ignited Spirit WIT"], "210061": ["Radiant Star"], "210062": ["Glittering Star"], "210071": ["I Wanna Win with You"], "210072": ["On the Way to Our Dream"], "210081": ["Past My Limits"], "210082": ["Eyes on the Goal"], "210091": ["Racing Spirit: Speed"], "210101": ["Racing Spirit: Stamina"], "210111": ["Racing Spirit: Power"], "210121": ["Racing Spirit: Guts"], "210131": ["Racing Spirit: Wit"], "210141": ["Racing Spirit: Mood"], "210261": ["Wisdom of the Sun"], "210262": ["Sun's Blessing"], "210271": ["Wisdom of the Sea"], "210272": ["Sea's Blessing"], "210281": ["Wisdom of the Earth"], "210282": ["Earth's Blessing"], "300011": ["Unquenched Thirst"], "300021": ["Unchanging"], "300031": ["Towards the Scenery I Seek"], "300041": ["Creeping Anxiety"], "300051": ["Blatant Fear"], "300061": ["Dream Run"], "300071": ["Show Me What Lies Beyond!"], "300081": ["Hoiya! Have a Good Run!"], "300091": ["As a Friend and Rival"], "300101": ["Cheers of a Fellow Dreamer"], "300111": ["Chin Up, Derby Umamusume!"], "300121": ["For the Team"], "300131": ["Ruler of Japan"], "300141": ["Indomitable"], "9000011": ["Carnival Bonus (inherited)"], "9000012": ["Carnival Bonus (inherited)"], "9000013": ["Carnival Bonus (inherited)"], "9000014": ["Carnival Bonus (inherited)"], "900011": ["Shooting Star"], "900021": ["The View from the Lead Is Mine!"], "900031": ["Sky-High Teio Step"], "900041": ["Red Shift/LP1211-M"], "900051": ["Lights of Vaudeville"], "900061": ["Triumphant Pulse"], "900071": ["Anchors Aweigh!"], "900081": ["Cut and Drive!"], "900091": ["Resplendent Red Ace"], "900101": ["Shooting for Victory!"], "900101111": ["Gluttonous Ruler (inherited)"], "900101211": ["Accelerating to My Dreams! (inherited)"], "900102111": ["Sunshine 'n' Spirit! (inherited)"], "900102211": ["Ruler of Summer (inherited)"], "900103111": ["Overwhelming Advance (inherited)"], "900103211": ["Gutsiest in Japan (inherited)"], "900111": ["Where There's a Will, There's a Way"], "900121": ["You and Me! One-on-One!"], "900131": ["The Duty of Dignity Calls"], "900141": ["Victoria por plancha \u2606"], "900151": ["This Dance Is for Vittoria!"], "900161": ["Shadow Break"], "900171": ["Behold Thine Emperor's Divine Might"], "900181": ["Blazing Pride"], "900191": ["OMG! (\uFF9F\u2200\uFF9F)  The Final Sprint! \u2606"], "900201": ["Angling and Scheming"], "900201111": ["Fully Focused (inherited)"], "900201211": ["Otherworldly Front-Runner (inherited)"], "900202111": ["Ultramarine Getaway (inherited)"], "900202211": ["R\xE1faga Azul (inherited)"], "900211": ["White Lightning Comin' Through!"], "900221": ["Fairy Tale"], "900231": ["\u2234win Q.E.D."], "900241": ["Flashy\u2606Landing"], "900251": ["Chasing After You"], "900261": ["G00 1st. F\u221E;"], "900271": ["Let's Pump Some Iron!"], "900281": ["YUMMY\u2606SPEED!"], "900291": ["Snow Bright, Snow Flight"], "900301": ["Blue Rose Closer"], "900301111": ["Talented Technique (inherited)"], "900301211": ["Teio Step (inherited)"], "900302111": ["Racing Prodigy (inherited)"], "900302211": ["To the Ends of the Earth (inherited)"], "900311": ["All Charged! It's Go Time!"], "900321": ["U=ma2"], "900331": ["Shooting Star of Dioskouroi"], "900341": ["Now We're Cruisin'!"], "900351": ["Our Ticket to Win!"], "900361": ["trigger:BEAT"], "900371": ["Schwarzes Schwert"], "900381": ["#LookatCurren"], "900391": ["A Princess Must Seize Victory!"], "900401": ["KEEP IT REAL."], "900401111": ["Let's Floor It, Baby! (inherited)"], "900401211": ["Revved Up Red-Hot (inherited)"], "900402111": ["You Can't Handle Me \u2606 (inherited)"], "900402211": ["Queen of the Stage (inherited)"], "900411": ["Genius x Bakushin = Victory"], "900421": ["I'm Possible!"], "900431": ["Ding Dong, Boo!"], "900441": ["Victory belongs to me\u2014Strelitzia! \u2606"], "900451": ["Pure Heart"], "900461": ["SPARKLY\u2606STARDOM"], "900471": ["Raise My Soul's Blade!"], "900481": ["Pop & Polish"], "900491": ["Laugh at the Odds"], "900501": ["Nemesis"], "900501111": ["Dazzling Star (inherited)"], "900501211": ["Entertainer (inherited)"], "900502111": ["Stunning Trick (inherited)"], "900502211": ["Glittering Star (inherited)"], "900511": ["Budding Blossom"], "900521": ["Super-Duper Climax"], "900531": ["Red-Hot Discipline!"], "900551": ["Magical\u2606Marvelous\u2605World"], "900561": ["I See Victory in My Future!"], "900571": ["Lyrical Journey"], "900581": ["I Never Goof Up!"], "900591": ["Moving Past, and Beyond"], "900601": ["Just a Little Farther!"], "900601111": ["Kasamatsu's Bottomless Pit (inherited)"], "900601211": ["Cornered by the Beast (inherited)"], "900602111": ["Full Belly, Full Energy (inherited)"], "900602211": ["Holiday Miracle (inherited)"], "900611": ["Prideful King"], "900621": ["Go, Go, Mun!"], "900641": ["Keep Pushing Ahead"], "900651": ["Hands in the Air Like Ya Don't Care!"], "900661": ["Turbo BLAST!"], "900671": ["Eternal Encompassing Shine"], "900681": ["Victory Cheer!"], "900691": ["Ambition to Surpass the Sakura"], "900701": ["Seirios"], "900701111": ["Her Lordship Seeth All (inherited)"], "900701211": ["It's All Goin' Downhill from Here! (inherited)"], "900702111": ["Golshi's Out-of-Body Experience (inherited)"], "900702211": ["Have Some Squid, Kid! (inherited)"], "90071": ["Warning Shot! (inherited)"], "900711": ["A Lifelong Dream, A Moment's Flight"], "900721": ["Peerless Dance of Flowering Flames"], "900741": ["Lovely Spring Breeze"], "900761": ["World in Bloom"], "900781": ["Sunny Breeze"], "900801111": ["A Swig of Strength (inherited)"], "900801211": ["Feet of Fury (inherited)"], "900802111": ["Cuttin' Through! (inherited)"], "900802211": ["Kick It into Top Gear! (inherited)"], "90081": ["Xceleration (inherited)"], "900831": ["Mission: Triumph"], "900841": ["Sublimated Thunder"], "900851": ["Ever Supreme"], "900871": ["Silent Letter"], "900901111": ["Perfectly Tuned (inherited)"], "900901211": ["First Place Is MINE! (inherited)"], "900902111": ["Rough Queen (inherited)"], "900902211": ["Door to My Dream (inherited)"], "90091": ["Red Ace (inherited)"], "900981": ["Luck Runs My Way"], "900991": ["Shine On, Tomakomai! \u2606"], "901001": ["Never Say Never"], "901001111": ["Shootin' Straight Ahead! (inherited)"], "901001211": ["Frontier Spirit (inherited)"], "901002111": ["Takin' Y'all on a Wild Ride! (inherited)"], "901002211": ["May the Best Uma Win! (inherited)"], "901041": ["Sunrise Banner\u2014Katsuragi Ace!"], "901051": ["Ad Astra"], "901061": ["Bang! Miracle Shot \u2606"], "901101111": ["Yamato Nadeshiko (inherited)"], "901101211": ["Indomitable Spirit (inherited)"], "901102111": ["Comforting Heal (inherited)"], "901102211": ["The Magic of Courage (inherited)"], "90111": ["Focused Mind (inherited)"], "901201111": ["Cry of Victory (inherited)"], "901201211": ["Pororoca Roar (inherited)"], "901202111": ["Engaging Engine! (inherited)"], "901202211": ["Cake-Cuttin' Time! (inherited)"], "901301111": ["As Befits a Mejiro (inherited)"], "901301211": ["Top-Class Composure (inherited)"], "901302111": ["Rigorous Rest (inherited)"], "901302211": ["Above the Cloudline (inherited)"], "901303111": ["Classy Summer (inherited)"], "901303211": ["An Earnest Breather (inherited)"], "901401111": ["Eyes of the Condor (inherited)"], "901401211": ["Performing Perfecto! (inherited)"], "901402111": ["Heroic Flurry (inherited)"], "901402211": ["Rising Condor (inherited)"], "90141": ["Coraz\xF3n \u2606 Ardiente (inherited)"], "901501111": ["Entrance of the Centurial Overlord! (inherited)"], "901501211": ["The Leading Actor (inherited)"], "901502111": ["Maestro of the Waltz (inherited)"], "901502211": ["Never a Beat Late! (inherited)"], "901601111": ["Decisive Strike (inherited)"], "901601211": ["In the Wolf's Maw (inherited)"], "901602111": ["Blazing Wolf (inherited)"], "901602211": ["Ravenous Beast (inherited)"], "901701111": ["Eyes of an Emperor (inherited)"], "901701211": ["Unquestioned Might (inherited)"], "901702111": ["Unwavering Expression (inherited)"], "901702211": ["Autumn Grace (inherited)"], "901801111": ["As the Empress Wills (inherited)"], "901801211": ["Untouchable Beauty (inherited)"], "901802111": ["Dignified Empress (inherited)"], "901802211": ["Moonlight Sommelier (inherited)"], "90181": ["Empress's Pride (inherited)"], "901901111": ["Ultimate Uma Simp (inherited)"], "901901211": ["Best View of Anyone! (inherited)"], "901902111": ["Run Like the Fic's Due Tomorrow! (inherited)"], "901902211": ["Back for More Moe! (inherited)"], "902001111": ["Quick Reel (inherited)"], "902001211": ["Breakaway Scheme (inherited)"], "902002111": ["Run If You Can \u266A (inherited)"], "902002211": ["Record Runner (inherited)"], "902101111": ["Gone in a Flash (inherited)"], "902101211": ["Paradise Tail (inherited)"], "902102111": ["A Prayer for Our Predecessors (inherited)"], "902102211": ["C'mon, Let's Do This! (inherited)"], "902201111": ["Royal Star (inherited)"], "902201211": ["Heavenly Descent (inherited)"], "902202111": ["Emboldening Memories (inherited)"], "902202211": ["Spear of Morr\xEDgan (inherited)"], "902301111": ["All According to Plan (inherited)"], "902301211": ["Strategic Cooldown (inherited)"], "902302111": ["Very Important Positioning (inherited)"], "902302211": ["Speedy Santa (inherited)"], "902401111": ["You copy? (inherited)"], "902401211": ["I copy! (inherited)"], "902402111": ["Soft White Whisper (inherited)"], "902402211": ["Barrel Roll! (inherited)"], "90241": ["1st Place Kiss\u2606 (inherited)"], "902501111": ["Coffee Break (inherited)"], "902501211": ["Don't Leave Me... (inherited)"], "902601111": ["Overclock (inherited)"], "902601211": ["Overcome Any Strife (inherited)"], "902602111": ["Malfunction Mitigation (inherited)"], "902602211": ["Perfect Boost (inherited)"], "902701111": ["Let Those Muscles SING! (inherited)"], "902701211": ["Bulk Up & Away! (inherited)"], "902702111": ["Helping Grant Your Dream (inherited)"], "902702211": ["Give You a Taste of Glory (inherited)"], "90271": ["Feel the Burn! (inherited)"], "902801111": ["Full Tank! (inherited)"], "902801211": ["Always Hungry for More! (inherited)"], "902901111": ["Like They Do in the City! (inherited)"], "902901211": ["Snow Gals Know Howta Sled! (inherited)"], "903001111": ["Heroic Determination (inherited)"], "903001211": ["Dark Assassin (inherited)"], "903002111": ["I Can Do This! (inherited)"], "903002211": ["Trick and Treat (inherited)"], "903101111": ["Stubborn Sis (inherited)"], "903101211": ["Wind God's Wrath (inherited)"], "903102111": ["Perfect \u266A Order Up! (inherited)"], "903102211": ["Slick Serving (inherited)"], "903201111": ["Latent One (inherited)"], "903201211": ["Efficient Rest Methodology (inherited)"], "903202111": ["Devil's Laplace (inherited)"], "903202211": ["Blinding Luxon (inherited)"], "90321": ["Introduction to Physiology (inherited)"], "903301111": ["Quiet Promise (inherited)"], "903301211": ["Self-Destructive Comet (inherited)"], "903401111": ["Firefighter's Ascent (inherited)"], "903401211": ["Expert on the Dirt (inherited)"], "903402111": ["Fan Dance (inherited)"], "903402211": ["Standout Step (inherited)"], "903501111": ["Never Ever Give Up (inherited)"], "903501211": ["Winning Road (inherited)"], "903502111": ["Superpowered Engines! (inherited)"], "903502211": ["I Can Fly! (inherited)"], "90351": ["V Is for Victory! (inherited)"], "903601111": ["Uplift (inherited)"], "903601211": ["Lose Myself (inherited)"], "903701111": ["Blinding Magia (inherited)"], "903701211": ["On My Honor (inherited)"], "903702111": ["Recipe for Victory (inherited)"], "903702211": ["Dark Flash (inherited)"], "903801111": ["#CuteChaos\u266A (inherited)"], "903801211": ["#GotYouDownBad\u266A (inherited)"], "903802111": ["#ThinkOfMe (inherited)"], "903802211": ["#BeautifulSprint (inherited)"], "903901111": ["Unstoppable Princess (inherited)"], "903901211": ["Rising Road Rage (inherited)"], "904001111": ["Pristine Foresight (inherited)"], "904001211": ["Star of the Runway (inherited)"], "904002111": ["Golden Formation (inherited)"], "904002211": ["Geared to Strike (inherited)"], "904101111": ["Bakushin SPRINT! (inherited)"], "904101211": ["Bakushin LEAD! (inherited)"], "904102111": ["Full Charge! BAKUSHIIIN! (inherited)"], "904102211": ["Baku-speed! BakuSHIIIIN! (inherited)"], "90411": ["Class Rep + Speed = Bakushin (inherited)"], "904201111": ["The World Is My Oyster (inherited)"], "904201211": ["Pearlescent Danceoff (inherited)"], "904301111": ["Just Run Like Crazy! (inherited)"], "904301211": ["Mischevious Maestro (inherited)"], "904401111": ["After-School Magical Girl (inherited)"], "904401211": ["Magic Charm (inherited)"], "904501111": ["Sweet Trance (inherited)"], "904501211": ["Motherly Maestro (inherited)"], "904502111": ["Expert Caretaker (inherited)"], "904502211": ["Mummy's Sweet Whispers (inherited)"], "90451": ["Clear Heart (inherited)"], "904601111": ["Umadol\u2605Onstage! (inherited)"], "904601211": ["Umadol \u266A Chart Topper! (inherited)"], "904602111": ["Rising to Umadol Stardom! (inherited)"], "904602211": ["Always Center Stage! (inherited)"], "904701111": ["Halfway to Heroism (inherited)"], "904701211": ["Shining Sword (inherited)"], "904801111": ["Lezzgo! (inherited)"], "904801211": ["Trendsetter (inherited)"], "904802111": ["I Can Totes Shake 'Em! (inherited)"], "904802211": ["I'm Literally the Best! (inherited)"], "904901111": ["It's All or Nothing (inherited)"], "904901211": ["Takin' Each Gamble (inherited)"], "905001111": ["Cutting-Edge Senses (inherited)"], "905001211": ["Heart of a Hunter (inherited)"], "905002111": ["I'll Show You! (inherited)"], "905002211": ["Bolt from the Brink (inherited)"], "905101111": ["Petals in the Breeze (inherited)"], "905101211": ["Blooming Resolve (inherited)"], "905102111": ["Flower Girl's Blessing (inherited)"], "905102211": ["Veil in the Spring Breeze (inherited)"], "905201111": ["I'll Keep Trying! (inherited)"], "905201211": ["I Won't Lose! (inherited)"], "905202111": ["Look, Look! (inherited)"], "905202211": ["I'll Do My Very Best! (inherited)"], "90521": ["Super-Duper Stoked (inherited)"], "905301111": ["Peak Discipline (inherited)"], "905301211": ["Ready for a Red-Hot Tech! (inherited)"], "905302111": ["Red-Hot Transforma-Runner! (inherited)"], "905302211": ["Ultimate Ultra-Sprint! (inherited)"], "905501111": ["Marvelous \u2606 Thrill (inherited)"], "905501211": ["Make the World Marvelous \u2606 (inherited)"], "905601111": ["Lucky Sign! (inherited)"], "905601211": ["Lucky Number Seven (inherited)"], "905602111": ["Crazy Extra Mega Lucky! (inherited)"], "905602211": ["Bring On the Luck! (inherited)"], "90561": ["Luck Be with Me! (inherited)"], "905701111": ["Soaring Steps (inherited)"], "905701211": ["Spring from the Earth (inherited)"], "905801111": ["Right on the Dot (inherited)"], "905801211": ["I-I'm Doing My Best! (inherited)"], "905802111": ["For My Rival! (inherited)"], "905802211": ["Soul Guide (inherited)"], "905901111": ["Cold Look (inherited)"], "905901211": ["Queen of Yodo (inherited)"], "905902111": ["I Want to Explore More! (inherited)"], "905902211": ["First Step from the Brink (inherited)"], "906001111": ["Cynical Charm (inherited)"], "906001211": ["Be the Lead! (inherited)"], "906002111": ["Just Gotta Do What I Can (inherited)"], "906002211": ["Keep the Cheers Comin' (inherited)"], "90601": ["I Can Win Sometimes, Right? (inherited)"], "906101111": ["Shocking Sweep (inherited)"], "906101211": ["Kingly View (inherited)"], "906102111": ["Ascend Glory (inherited)"], "906102211": ["Sakura Blossoms in the Breeze (inherited)"], "90611": ["Call Me King (inherited)"], "906201111": ["Goin' My Way! (inherited)"], "906201211": ["Ei, Ei, Mun! (inherited)"], "906202111": ["Positive Smile (inherited)"], "906202211": ["Turning Things Right Around! (inherited)"], "90621": ["Ready, Go! (inherited)"], "906401111": ["WILD, Amirite? (inherited)"], "906401211": ["Breakaway on Lock (inherited)"], "906501111": ["LOL I'm on FIRE! (inherited)"], "906501211": ["DW I'm on It! (inherited)"], "906601111": ["1000% Output! (inherited)"], "906601211": ["Turbo's Leadin' the Charge! (inherited)"], "90661": ["Engines LIT! (inherited)"], "906701111": ["Indestructable (inherited)"], "906701211": ["Rondo to the Right (inherited)"], "906702111": ["Beyond My Station (inherited)"], "906702211": ["The Dawn Always Comes! (inherited)"], "906801111": ["Get the Festival Going! (inherited)"], "906801211": ["Bright Blast (inherited)"], "906802111": ["Leap Ahead to the Future (inherited)"], "906802211": ["Introducing the Victor! (inherited)"], "906901111": ["Good Year for Sakura! (inherited)"], "906901211": ["The Sakura Bloom Regally Today (inherited)"], "906902111": ["Bloom Down the Branch (inherited)"], "906902211": ["I Don't Wanna Lose! (inherited)"], "907001111": ["Unyielding Aster (inherited)"], "907001211": ["Arabesque of the Brightest Star (inherited)"], "907101111": ["Eternal Light (inherited)"], "907101211": ["Give This Moment Everything (inherited)"], "907102111": ["Elegant Phosphorescence (inherited)"], "907102211": ["Fleeting Afterglow (inherited)"], "907201111": ["Growing Flame (inherited)"], "907201211": ["Yaeno's Fighting Spirit (inherited)"], "907401111": ["Ladylike Resolve (inherited)"], "907401211": ["Immovable as a Mountain (inherited)"], "907601111": ["Open All the Buds (inherited)"], "907601211": ["Blossom for Us! (inherited)"], "907801111": ["Shearing Gust (inherited)"], "907801211": ["As Nature Wills (inherited)"], "908301111": ["Simple and Sturdy (inherited)"], "908301211": ["Imposing Innovation (inherited)"], "908401111": ["The Stormcaller of Destruction (inherited)"], "908401211": ["Intoxicate the World (inherited)"], "908501111": ["Ever Graceful (inherited)"], "908501211": ["Always the Most Brilliant (inherited)"], "908701111": ["Raise the Pitch (inherited)"], "908701211": ["Leave a Footprint in Your Memory (inherited)"], "909801111": ["Lucky Rickey! (inherited)"], "909801211": ["Longmai Pulse (inherited)"], "909901111": ["Thinkin' About Tomakomai Food (inherited)"], "909901211": ["Be a Good Ambassador! (inherited)"], "9100011": ["Feelin' a Bit Silly (inherited)"], "910001111": ["A Dose of Ambition (inherited)"], "910001211": ["Divine on the Dirt (inherited)"], "910011": ["Dazzl'n \u266A Diver"], "910021": ["Ahead of the Horizon"], "910031": ["Certain Victory"], "910041": ["A Kiss for Courage"], "910051": ["Ravissant"], "910061": ["Festive Miracle"], "910071": ["564 Escapades"], "910081": ["Into High Gear!"], "910091": ["Queen's Lumination"], "910101": ["Joyful Voyage!"], "910111": ["Superior Heal"], "910121": ["First Bite of the Feast!"], "910131": ["Legacy of the Strong"], "910141": ["Condor's Fury"], "910151": ["Barcarole of Blessings"], "910161": ["Free From the Ashes"], "910171": ["Arrows Whistle, Shadows Disperse"], "910181": ["Eternal Moments"], "910191": ["THE MOE AAAA Thanks for My Life"], "910201": ["Break It Down!"], "910211": ["Lightning Flare"], "910221": ["Best day ever"], "910231": ["Presents from X"], "910241": ["Flowery\u2606Maneuver"], "910261": ["Operation Cacao"], "910271": ["A Warm Cup for You"], "910301": ["Every Rose Has Its Fangs"], "910311": ["Fresh\u2606Parlor"], "910321": ["Summer Halation"], "910341": ["Firelight"], "910351": ["Ticket to Your Dreams!"], "910371": ["Guten Appetit \u266A"], "910381": ["One True Color"], "910401": ["Dancing in the Leaves"], "910401111": ["Aced It! (inherited)"], "910401211": ["Runnin' the Race Solo (inherited)"], "910411": ["Cherry\u2606Scramble"], "910451": ["Give Mummy a Hug \u2661"], "910461": ["\u03B1-star*"], "910481": ["Us Girlies Keep Winnin'! \u266A"], "910501": ["Hephaestus"], "910501111": ["Beyond the Stars with You (inherited)"], "910501211": ["Cross the Sea of Stars (inherited)"], "910511": ["Flowering Dreams"], "910521": ["114th Time's the Charm"], "910531": ["Scorching Summer Tech!"], "910561": ["Bountiful Harvest"], "910581": ["Spooky, Scary, Happy"], "910591": ["Wherever This Wonder Leads"], "910601": ["Go\u2606Go\u2606Goal!"], "910601111": ["No Pressure, No Rush (inherited)"], "910601211": ["Collect All the Little Miracles (inherited)"], "910611": ["Louder! Tracen Cheer!"], "910621": ["Tumbly Power Drive!"], "910671": ["Rain Cloud Bolt"], "910681": ["Bring on the Banquet!"], "910691": ["Watch! Me! BLOOM!"], "910711": ["Danser le Pr\xE9sent"], "9200011": ["Medium Race Enthusiast (inherited)"], "9200021": ["Long Race Enthusiast (inherited)"], "9200031": ["Mile Race Enthusiast (inherited)"], "9200041": ["Sprint Race Enthusiast (inherited)"], "9200051": ["Dirt Race Enthusiast (inherited)"], "920011": ["Dreams Donned with Pride!"], "920131": ["Your Smile Sparkles as the Waves"], "9300011": ["Energetic Hero (inherited)"], "9300021": ["Sunny Hero (inherited)"], "9300031": ["Rainy Hero (inherited)"], "9300041": ["Inner Hero (inherited)"], "9300051": ["Outer Hero (inherited)"], "9300061": ["Friendly Hero (inherited)"], "9300071": ["Dashing Hero (inherited)"] };

  // vendor/kachi-uma-tools/umalator-global/tracknames.json
  var tracknames_default = {
    "10001": ["", "Sapporo"],
    "10002": ["", "Hakodate"],
    "10003": ["", "Niigata"],
    "10004": ["", "Fukushima"],
    "10005": ["", "Nakayama"],
    "10006": ["", "Tokyo"],
    "10007": ["", "Chukyo"],
    "10008": ["", "Kyoto"],
    "10009": ["", "Hanshin"],
    "10010": ["", "Kokura"],
    "10101": ["", "Ooi"]
  };

  // adapter.ts
  var MODEL_COMMIT = "4401e25987d9c78707f2d3f8a9dcd02c1c56529c";
  var trackCn = {
    "10001": "\u672D\u5E4C",
    "10002": "\u51FD\u9986",
    "10003": "\u65B0\u6F5F",
    "10004": "\u798F\u5C9B",
    "10005": "\u4E2D\u5C71",
    "10006": "\u4E1C\u4EAC",
    "10007": "\u4E2D\u4EAC",
    "10008": "\u4EAC\u90FD",
    "10009": "\u962A\u795E",
    "10010": "\u5C0F\u4ED3",
    "10101": "\u5927\u4E95",
    "10103": "\u5DDD\u5D0E",
    "10104": "\u8239\u6865",
    "10105": "\u76DB\u5188",
    "10201": "\u9686\u5C1A"
  };
  var baseAssumptions = [
    "\u53C2\u8003\u673A\u5236\u6A21\u578B\uFF0C\u4E0D\u662F\u5B8C\u6574\u9A6C\u7FA4\u80DC\u7387\u6A21\u62DF\u3002",
    "\u4F4D\u7F6E\u3001\u6392\u540D\u53CA\u5BF9\u624B\u76F8\u5173\u6280\u80FD\u91C7\u7528\u8BE5\u5F15\u64CE\u7684\u8FD1\u4F3C\u6761\u4EF6\u3002",
    "\u4E94\u7EF4\u6309\u672C\u5F15\u64CE\u7684\u7A81\u78341200\u6298\u7B97\u3001\u5FC3\u60C5\u3001\u9002\u6027\u53CA\u8D5B\u9053\u4FEE\u6B63\u5904\u7406\u3002",
    "\u9A6C\u8EAB\u4E3A\u5728\u5148\u5230\u7EC8\u70B9\u4E00\u65B9\u7684\u7ED3\u675F\u65F6\u523B\uFF0C\u53CC\u65B9\u8DDD\u79BB\u5DEE\u9664\u4EE52.5\u7C73\u3002"
  ];
  function metadata(args) {
    const requested = String(args.server || args.requested_server || "global");
    const reference = !["jp", "global", "en", "ja"].includes(requested.toLowerCase());
    return {
      model: "kachi-reference-source",
      model_commit: MODEL_COMMIT,
      data_commit: MODEL_COMMIT,
      data_snapshot: `kachi-${MODEL_COMMIT.slice(0, 8)}-${"global"}`,
      engine_ruleset: "global",
      requested_server: requested,
      data_scope: "global",
      status: reference ? "reference_for_requested_server" : "reference_model",
      cn_calibrated: false,
      tw_calibrated: false,
      source_built: true,
      full_field_win_probability_modeled: false,
      assumptions: [...reference ? [`\u8BF7\u6C42\u670D\u522B${requested}\u76EE\u524D\u4F7F\u7528\u660E\u786E\u6807\u6CE8\u7684${"global"}\u53C2\u8003\u673A\u5236\uFF0C\u5C1A\u672A\u5B8C\u6210\u8BE5\u670D\u6821\u51C6\u3002`] : [], ...baseAssumptions]
    };
  }
  function skillVariant(id, value) {
    if (id.startsWith("9") && id.length >= 6)
      return "inherited_unique";
    if (value.rarity >= 3 && value.rarity <= 5)
      return "native_unique";
    if (value.rarity === 6)
      return "evolution";
    return "normal";
  }
  function skillEntry(id) {
    const v = skill_data_default[id];
    const names = skillnames_default[id] || [];
    return {
      db_id: id,
      skill_id: id,
      name: names[0] || names[1] || id,
      name_ja: names[0] || null,
      name_en: names[1] || null,
      variant: skillVariant(id, v),
      rarity: v.rarity,
      source: "global game-data snapshot in kachi fork",
      source_commit: MODEL_COMMIT,
      ruleset: "global",
      simulatable_record: true,
      mechanics: v.alternatives
    };
  }
  function skillCatalog(args) {
    const query = String(args.query || "").toLowerCase();
    const ids = args.ids ? new Set(args.ids.map(String)) : null;
    const limit = Math.min(500, Math.max(1, Number(args.limit) || 40));
    const found = Object.keys(skill_data_default).filter((id) => (!ids || ids.has(id)) && (!query || [id, ...skillnames_default[id] || []].join(" ").toLowerCase().includes(query)));
    return { ok: true, action: "simulation.skills", result: { skills: found.slice(0, limit).map(skillEntry), total: found.length, limit }, metadata: metadata(args) };
  }
  function courseEntry(id) {
    const c = course_data_default[id];
    const names = tracknames_default[String(c.raceTrackId)] || [];
    const venue = trackCn[String(c.raceTrackId)] || names[0] || names[1] || String(c.raceTrackId);
    return {
      course_id: id,
      venue,
      venue_ja: names[0] || null,
      venue_en: names[1] || null,
      distance: c.distance,
      surface: c.surface === 1 ? "turf" : "dirt",
      surface_label: c.surface === 1 ? "\u8349\u5730" : "\u6CE5\u5730",
      direction: c.turn === 1 ? "clockwise" : c.turn === 2 ? "counterclockwise" : "straight",
      direction_label: c.turn === 1 ? "\u987A\u65F6\u9488" : c.turn === 2 ? "\u9006\u65F6\u9488" : "\u76F4\u7EBF",
      ruleset: "global",
      name: `${venue} ${c.surface === 1 ? "\u8349\u5730" : "\u6CE5\u5730"}${c.distance}\u7C73 ${c.turn === 1 ? "\u987A\u65F6\u9488" : c.turn === 2 ? "\u9006\u65F6\u9488" : "\u76F4\u7EBF"}`,
      source_commit: MODEL_COMMIT
    };
  }
  function courseCatalog(args) {
    const original = String(args.query || "").toLowerCase();
    const eventAlias = /菊花[赏賞]|kikuka\s*sho/.test(original);
    const query = original.replace(/菊花[赏賞]|kikuka\s*sho/g, "10810").replace(/(\d+(?:\.\d+)?)\s*(?:km|公里|千米)/g, (_all, n) => String(Number(n) * 1e3)).replace(/(\d+)\s*(?:米|m\b|メートル|meters?\b)/g, "$1 ").replace(/(\d+)/g, " $1 ");
    const tokens = query.split(/[\s,，/·]+/).filter(Boolean);
    const values = Object.keys(course_data_default).map(courseEntry).filter((c) => tokens.every((token) => {
      if (/^\d+$/.test(token))
        return String(c.distance) === token || c.course_id === token;
      if (["turf", "\u829D", "\u8349\u5730"].includes(token))
        return c.surface === "turf";
      if (["dirt", "\u30C0\u30FC\u30C8", "\u6CE5\u5730"].includes(token))
        return c.surface === "dirt";
      if (["clockwise", "\u987A\u65F6\u9488", "\u53F3\u56DE", "\u53F3\u56DE\u308A"].includes(token))
        return c.direction === "clockwise";
      if (["counterclockwise", "\u9006\u65F6\u9488", "\u5DE6\u56DE", "\u5DE6\u56DE\u308A"].includes(token))
        return c.direction === "counterclockwise";
      if (["straight", "\u76F4\u7EBF", "\u76F4\u7DDA"].includes(token))
        return c.direction === "straight";
      return [c.name, c.venue_ja, c.venue_en, c.course_id].join(" ").toLowerCase().includes(token);
    }));
    const meta = metadata(args);
    if (eventAlias)
      meta.assumptions.push("\u83CA\u82B1\u8D4F\u522B\u540D\u4EC5\u5339\u914D\u4EAC\u90FD\u8349\u57303000\u7C73\u7684\u8D5B\u9053\u51E0\u4F55\u5165\u53E3\uFF0C\u672A\u63A8\u5B9A\u5177\u4F53\u5C4A\u6B21\u3001\u5929\u6C14\u6216\u8D5B\u671F\u3002");
    return { ok: true, action: "simulation.courses", result: {
      courses: values,
      total: values.length,
      query_tokens: tokens,
      alias_geometry_only: eventAlias
    }, metadata: meta };
  }
  var strategies = {
    "\u9003": "Nige",
    "\u9003\u9A6C": "Nige",
    "\u9003\u3052": "Nige",
    "front": "Nige",
    "nige": "Nige",
    "\u5148": "Senkou",
    "\u5148\u884C": "Senkou",
    "pace": "Senkou",
    "senko": "Senkou",
    "senkou": "Senkou",
    "\u5DEE": "Sasi",
    "\u5DEE\u884C": "Sasi",
    "\u5DEE\u3057": "Sasi",
    "late": "Sasi",
    "sashi": "Sasi",
    "sasi": "Sasi",
    "\u8FFD": "Oikomi",
    "\u8FFD\u9A6C": "Oikomi",
    "\u8FFD\u5165": "Oikomi",
    "\u8FFD\u8FBC": "Oikomi",
    "\u8FFD\u8FBC\u307F": "Oikomi",
    "end": "Oikomi",
    "oikomi": "Oikomi",
    "\u5927\u9003": "Oonige",
    "oonige": "Oonige"
  };
  function statsSummary(values) {
    const a = [...values].sort((x, y) => x - y);
    const n = a.length;
    const mid = Math.floor(n / 2);
    return { mean: a.reduce((x, y) => x + y, 0) / n, median: n % 2 ? a[mid] : (a[mid - 1] + a[mid]) / 2, min: a[0], max: a[n - 1] };
  }
  function inputHorse(args, defaults) {
    const ref = { speed: 1200, stamina: 1200, power: 1e3, guts: 800, wisdom: 1e3 };
    const stats = args.stats || {};
    const result = {};
    for (const key of Object.keys(ref)) {
      const given = stats[key];
      if (given === void 0 || given === null || given === "") {
        result[key] = ref[key];
        defaults.push(`\u53C2\u8003${key}=${ref[key]}`);
      } else {
        const n = Number(given);
        if (!Number.isFinite(n) || n < 1)
          throw new Error(`\u4E94\u7EF4${key}\u9700\u8981\u6B63\u6570\u3002`);
        result[key] = n;
      }
    }
    const style = String(args.strategy || "\u5148\u884C");
    if (!args.strategy)
      defaults.push("\u53C2\u8003\u8DD1\u6CD5=\u5148\u884C");
    result.strategy = strategies[style.toLowerCase()] || style;
    result.distanceAptitude = args.distance_aptitude || "A";
    result.surfaceAptitude = args.surface_aptitude || "A";
    result.strategyAptitude = args.strategy_aptitude || "A";
    if (!args.distance_aptitude || !args.surface_aptitude || !args.strategy_aptitude)
      defaults.push("\u672A\u586B\u5199\u7684\u8DDD\u79BB/\u573A\u5730/\u8DD1\u6CD5\u9002\u6027\u53C2\u8003A\u3002");
    const moods = { "\u7EDD\u597D\u8C03": 2, "\u7EDD\u4F73": 2, "\u597D\u8C03": 1, "\u4E0A\u4F73": 1, "\u666E\u901A": 0, "normal": 0, "\u4E0D\u8C03": -1, "\u4E0D\u4F73": -1, "\u7EDD\u4E0D\u8C03": -2, "\u6781\u5DEE": -2 };
    result.mood = typeof args.mood === "string" && args.mood in moods ? moods[args.mood] : Number(args.mood ?? 0);
    if (args.mood === void 0)
      defaults.push("\u53C2\u8003\u5FC3\u60C5=\u666E\u901A");
    return result;
  }
  function makeBuilder(nsamples, course, horse, ids, args, onActivate) {
    const b = new RaceSolverBuilder(nsamples).seed(Number(args.seed ?? 20261025)).course(course).horse(horse).ground(args.ground || "good").weather(args.weather || "sunny").season(args.season || "autumn").time(args.time || "midday").order(Number(args.order_min ?? 1), Number(args.order_max ?? args.field_size ?? 9)).numUmas(Number(args.field_size ?? 9)).posKeepMode(args.position_keep === "approximate" ? 1 /* Approximate */ : 0 /* None */).mode("compare").skillWisdomCheck(args.skill_activation_checks !== false).rushedKakari(args.rushed !== false).competeFight(false).leadCompetition(false).laneMovement(false);
    for (const id of ids)
      b.addSkill(id);
    if (false)
      b.withAsiwotameru().withStaminaSyoubu();
    b.onSkillActivate(onActivate);
    return b;
  }
  function racePair(base, variant, distance) {
    const dt = 1 / 30;
    let t = 0, firstGap = null;
    let timeA = null, timeB = null;
    let hpA = null, hpB = null;
    let spurtA = false, spurtB = false;
    for (let step = 0; step < 12e4 && (timeA === null || timeB === null); step++) {
      const pa = base.pos, pb = variant.pos;
      if (timeA === null)
        base.step(dt);
      if (timeB === null)
        variant.step(dt);
      const xa = base.pos, xb = variant.pos;
      if (![xa, xb, base.currentSpeed, variant.currentSpeed].every(Number.isFinite))
        throw new Error("\u8BE5\u6280\u80FD/\u673A\u5236\u4EA7\u751F\u4E86\u65E0\u6548\u6570\u503C\uFF0C\u5F53\u524D\u6A21\u578B\u4E0D\u652F\u6301\u6B64\u7EC4\u5408\u3002");
      const fa = timeA === null && xa >= distance ? (distance - pa) / (xa - pa) : Infinity;
      const fb = timeB === null && xb >= distance ? (distance - pb) / (xb - pb) : Infinity;
      const f = Math.min(fa, fb);
      if (firstGap === null && Number.isFinite(f))
        firstGap = (pb + (xb - pb) * f - (pa + (xa - pa) * f)) / 2.5;
      if (Number.isFinite(fa)) {
        timeA = t + dt * fa;
        hpA = base.hp.hp;
        spurtA = !!base.hp.isMaxSpurt?.();
      }
      if (Number.isFinite(fb)) {
        timeB = t + dt * fb;
        hpB = variant.hp.hp;
        spurtB = !!variant.hp.isMaxSpurt?.();
      }
      t += dt;
    }
    base.cleanup();
    variant.cleanup();
    if (timeA === null || timeB === null)
      throw new Error("\u5F53\u524D\u53C2\u8003\u6A21\u62DF\u672A\u80FD\u7ED3\u675F\u3002");
    return { gain: firstGap || 0, time_a: timeA, time_b: timeB, hp_a: hpA, hp_b: hpB, spurt_a: spurtA, spurt_b: spurtB };
  }
  function run(args) {
    const id = String(args.course_id || "");
    const course = course_data_default[id];
    if (!course)
      return { ok: true, action: "simulation.run", result: { status: "needs_course", courses: Object.keys(course_data_default).slice(0, 8).map(courseEntry), message: "\u8BF7\u5148\u9009\u62E9\u8D5B\u9053\uFF1B\u8D44\u6599\u67E5\u8BE2\u4ECD\u53EF\u4F7F\u7528\u3002" }, metadata: metadata(args) };
    const assumptions = [];
    const horse = inputHorse(args, assumptions);
    const requested = Number(args.samples) || 64;
    const samples = Math.min(1e3, Math.max(1, Math.floor(requested)));
    if (samples !== requested)
      assumptions.push(`\u6837\u672C\u6570\u8C03\u6574\u4E3A${samples}\u4EE5\u63A7\u5236\u672C\u5730\u8BA1\u7B97\u65F6\u95F4\u3002`);
    if (!args.ground || !args.weather || !args.season || !args.field_size)
      assumptions.push("\u672A\u586B\u5199\u6761\u4EF6\u91C7\u7528\u53C2\u8003\u826F\u573A\u3001\u6674\u3001\u79CB\u5B63\u548C9\u4EBA\uFF1B\u6CA1\u6709\u5B8C\u6574\u5BF9\u624B\u961F\u4F0D\u5EFA\u6A21\u3002");
    const sourceSkills = Array.isArray(args.skills) ? args.skills : String(args.skills || "").split(/[,，\s]+/).filter(Boolean);
    const all = sourceSkills.map((v) => String(typeof v === "object" ? v.db_id ?? v.id : v));
    const unavailable = all.filter((v) => !skill_data_default[v]);
    const baseline = [...new Set(all.filter((v) => skill_data_default[v]))];
    let compareId = String(args.compare_skill_id || "");
    if (compareId && !skill_data_default[compareId])
      return { ok: true, action: "simulation.run", result: { status: "skill_not_in_snapshot", skill_id: compareId, message: "\u5F53\u524D\u53C2\u8003\u5F15\u64CE\u5FEB\u7167\u6CA1\u6709\u8FD9\u4E2A\u6280\u80FD\u3002\u6280\u80FD\u8D44\u6599\u4ECD\u53EF\u72EC\u7ACB\u67E5\u8BE2\u3002" }, metadata: metadata(args) };
    const compared = compareId ? [.../* @__PURE__ */ new Set([...baseline, compareId])] : baseline;
    let activatedSamples = 0, active = false;
    const activationExamples = [];
    const a = makeBuilder(samples, course, horse, baseline, args, () => {
    }).build();
    const b = makeBuilder(samples, course, horse, compared, args, (state, skillId) => {
      if (skillId !== compareId)
        return;
      active = true;
      if (activationExamples.length < 5) {
        const effects = (name) => (state[name] || []).filter((row) => row.skillId === skillId).map((row) => ({ modifier: row.modifier, duration_seconds: -row.durationTimer.t }));
        activationExamples.push({
          skill_id: skillId,
          position_m: state.pos,
          time_seconds: state.accumulatetime.t,
          current_speed: effects("activeCurrentSpeedSkills"),
          target_speed: effects("activeTargetSpeedSkills"),
          acceleration: effects("activeAccelSkills")
        });
      }
    }).build();
    const rows = [];
    for (let i = 0; i < samples; i++) {
      active = false;
      const left = a.next().value, right = b.next().value;
      rows.push(racePair(left, right, course.distance));
      if (active)
        activatedSamples++;
    }
    const baseResult = {
      finish_time_seconds: statsSummary(rows.map((r) => r.time_a)),
      remaining_hp: statsSummary(rows.map((r) => r.hp_a)),
      full_spurt_rate: rows.filter((r) => r.spurt_a).length / samples,
      hp_positive_finish_rate: rows.filter((r) => r.hp_a > 0).length / samples
    };
    const otherResult = {
      finish_time_seconds: statsSummary(rows.map((r) => r.time_b)),
      remaining_hp: statsSummary(rows.map((r) => r.hp_b)),
      full_spurt_rate: rows.filter((r) => r.spurt_b).length / samples,
      hp_positive_finish_rate: rows.filter((r) => r.hp_b > 0).length / samples
    };
    const meta = metadata(args);
    meta.assumptions.push(...assumptions);
    if (unavailable.length)
      meta.assumptions.push(`\u5FEB\u7167\u4E2D\u6CA1\u6709\u7684\u5DF2\u5B66\u6280\u80FD\u672A\u7EB3\u5165\uFF1A${unavailable.join("\u3001")}\u3002`);
    meta.assumptions.push("\u56FA\u5B9A\u6B65\u957F1/30\u79D2\uFF1B\u51B2\u523A\u7387/\u5269\u4F59HP\u6765\u81EA\u5F15\u64CEHP\u6A21\u578B\uFF0C\u4E0D\u80FD\u4F5C\u4E3A\u771F\u5B9E\u6BD4\u8D5B\u80DC\u7387\u3002");
    if (samples < 50)
      meta.assumptions.push("\u5F53\u524D\u6837\u672C\u8F83\u5C11\uFF0C\u533A\u95F4\u548C\u5747\u503C\u53EF\u80FD\u4E0D\u7A33\u5B9A\uFF1B\u53EF\u589E\u52A0\u6837\u672C\u518D\u6BD4\u8F83\u3002");
    if (compareId && skillVariant(compareId, skill_data_default[compareId]) === "native_unique")
      meta.assumptions.push("\u56FA\u6709\u6280\u80FD\u91C7\u7528\u8BE5\u5FEB\u7167\u8BB0\u5F55\u7684\u57FA\u7840\u6548\u679C\uFF1B\u5F53\u524D\u9002\u914D\u5668\u5C1A\u672A\u989D\u5916\u91CD\u7B97\u56FA\u6709\u7B49\u7EA7\u63D0\u5347\u3002");
    return { ok: true, action: "simulation.run", result: {
      status: assumptions.length ? "reference_inputs" : "reference_result",
      course: courseEntry(id),
      inputs: {
        stats: horse,
        effective_stats: buildAdjustedStats(buildBaseStats(horse, horse.mood), course, parseGroundCondition(args.ground || "good")),
        strategy: horse.strategy,
        skills: baseline,
        compare_skill_id: compareId || null,
        samples,
        seed: Number(args.seed ?? 20261025),
        skill_activation_checks: args.skill_activation_checks !== false,
        position_keep: args.position_keep || "none",
        unique_level_requested: args.unique_level ?? 1,
        unique_level_modeled: false,
        conditions: { ground: args.ground || "good", weather: args.weather || "sunny", season: args.season || "autumn", time: args.time || "midday", mood: horse.mood, field_size: Number(args.field_size ?? 9) }
      },
      baseline: baseResult,
      comparison: compareId ? otherResult : null,
      skill_delta: compareId ? {
        skill: skillEntry(compareId),
        bashin: statsSummary(rows.map((r) => r.gain)),
        time_saved_seconds: statsSummary(rows.map((r) => r.time_a - r.time_b)),
        activated_samples: activatedSamples,
        activation_rate_observed: activatedSamples / samples,
        activation_examples: activationExamples,
        already_in_baseline: baseline.includes(compareId)
      } : null,
      finish_viability: {
        kind: "reference_hp_and_spurt_only",
        baseline_full_spurt_rate: baseResult.full_spurt_rate,
        comparison_full_spurt_rate: compareId ? otherResult.full_spurt_rate : null,
        win_probability: null
      },
      unavailable_skills: unavailable
    }, metadata: meta };
  }
  function dispatch(args) {
    try {
      const conditions = {
        ground: { "\u826F": "good", "\u7A0D\u91CD": "yielding", "\u91CD": "soft", "\u4E0D\u826F": "heavy" },
        weather: { "\u6674": "sunny", "\u9634": "cloudy", "\u96E8": "rainy", "\u96EA": "snowy", "rain": "rainy", "snow": "snowy" },
        season: { "\u6625": "spring", "\u590F": "summer", "\u79CB": "autumn", "\u51AC": "winter", "\u6A31\u82B1": "sakura" },
        time: { "\u4E0A\u5348": "morning", "\u767D\u5929": "midday", "\u4E2D\u5348": "midday", "\u508D\u665A": "evening", "\u591C\u95F4": "night" }
      };
      args = { ...args };
      for (const field of Object.keys(conditions))
        if (args[field] in conditions[field])
          args[field] = conditions[field][args[field]];
      switch (args.action || args.operation || args.method) {
        case "simulation.courses":
        case "courses":
          return courseCatalog(args);
        case "simulation.skills":
        case "skills":
          return skillCatalog(args);
        case "simulation.run":
        case "run":
          return run(args);
        default:
          return { ok: false, error: "unknown_simulation_action" };
      }
    } catch (error) {
      return { ok: false, error: "simulation_model_error", message: String(error instanceof Error ? error.message : error), metadata: metadata(args) };
    }
  }

  // worker-entry.ts
  self.addEventListener("message", (event) => {
    self.postMessage({ id: event.data?.id, result: dispatch(event.data?.request || event.data) });
  });
})();
