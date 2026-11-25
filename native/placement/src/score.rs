use crate::{model::Leg, PlacementState};

// Python 3.12 switched built-in sum() to compensated summation. Terms that
// use explicit += in the reference objective continue to accumulate in order.
fn ordered_sum(values: impl Iterator<Item = f64>, accurate: bool) -> f64 {
    let mut total: f64 = 0.;
    let mut correction = 0.;
    for value in values {
        let next = total + value;
        if accurate {
            correction += if total.abs() >= value.abs() {
                (total - next) + value
            } else {
                (value - next) + total
            };
        }
        total = next;
    }
    total + correction
}

impl PlacementState {
    fn leg(&self, pairs: &Leg) -> Option<f64> {
        pairs
            .iter()
            .map(|&(a, b)| {
                let (x, y) = self.world[a];
                let (u, v) = self.world[b];
                (x - u).hypot(y - v)
            })
            .reduce(f64::min)
    }

    fn family_costs(&self) -> (f64, f64) {
        let (mut affinity, mut clearance) = (0., 0.);
        for f in &self.model.families {
            let Some(j) = f.parent else {
                affinity += 10000.;
                continue;
            };
            let a = self.poses[f.child];
            let b = self.poses[j];
            let d = f
                .shared
                .iter()
                .filter_map(|leg| self.leg(leg))
                .reduce(f64::max)
                .unwrap_or_else(|| a.distance(b));
            let (weight, pref) = if f.critical { (35., 4.) } else { (12., 8.) };
            affinity += d * weight;
            if d > pref {
                affinity += (d - pref).powi(2) * weight * 5.;
            }
            if let (Some(s), Some(g)) = (self.leg(&f.supplies), self.leg(&f.grounds)) {
                affinity += 20. * (s.min(12.).powi(2) + g.min(12.).powi(2));
            } else if let Some(pin) = self.leg(&f.owner_pin) {
                affinity += 20. * pin.min(12.).powi(2);
            }
            if !self.opposite_surfaces(f.child, j) {
                let dx = ((a.x - b.x).abs() - (a.w + b.w) / 2.).max(0.);
                let dy = ((a.y - b.y).abs() - (a.h + b.h) / 2.).max(0.);
                clearance += (self.model.support_gap - dx.hypot(dy)).max(0.).powi(2) * 10000.;
            }
        }
        for leg in &self.model.output_links {
            if let Some(d) = self.leg(leg) {
                affinity += 16. * d.min(12.).powi(2);
            }
        }
        (affinity, clearance)
    }

    fn grouped(&self) -> (f64, f64) {
        let mut grouping = 0.;
        for members in &self.model.groups {
            let distances = members.iter().enumerate().flat_map(|(i, &a)| {
                members[i + 1..]
                    .iter()
                    .map(move |&b| self.poses[a].distance(self.poses[b]).min(20.).powi(2))
            });
            grouping +=
                ordered_sum(distances, self.model.accurate_sum) / (members.len() - 1) as f64 * 0.2;
        }
        let mut alignment = 0.;
        for (members, power) in &self.model.peers {
            let mut xs = Bounds::default();
            for &i in members {
                xs.add(self.poses[i].x, self.poses[i].y);
            }
            let align_y = *power || xs.right - xs.left >= xs.bottom - xs.top;
            let mut group = 0.;
            for (i, &a) in members.iter().enumerate() {
                for &b in &members[i + 1..] {
                    let left = self.poses[a];
                    let right = self.poses[b];
                    let lateral = if align_y {
                        (left.y - right.y).abs()
                    } else {
                        (left.x - right.x).abs()
                    };
                    group += left.distance(right).min(10.).powi(2) * 0.05
                        + lateral.min(10.).powi(2) * if *power { 4. } else { 0.5 };
                }
            }
            alignment += group / (members.len() - 1) as f64;
        }
        (grouping, alignment)
    }

    fn connectivity(&self) -> f64 {
        let mut total = 0.;
        if self.model.pad_mode {
            for (weight, ids) in &self.model.nets {
                let mut b = Bounds::default();
                for &id in ids {
                    let (x, y) = self.world[id];
                    b.add(x, y);
                }
                let hpwl = b.right - b.left + b.bottom - b.top;
                total += hpwl * hpwl * weight / (ids.len() - 1) as f64;
            }
        } else {
            for &(a, b, w) in &self.model.edges {
                let a = self.poses[a];
                let b = self.poses[b];
                total += ((a.x - b.x).powi(2) + (a.y - b.y).powi(2)) * w;
            }
        }
        total * 0.2
    }

    fn compactness(&self) -> f64 {
        let mut bounds = Bounds::default();
        let mut content = Vec::new();
        let mut owners = Vec::new();
        for (part, p) in self.model.parts.iter().zip(&self.poses) {
            if part.edge || part.locked {
                continue;
            }
            content.push(p);
            if !part.support {
                owners.push(p);
            }
            bounds.add(p.body.left, p.body.top);
            bounds.add(p.body.right, p.body.bottom);
        }
        if content.len() < 2 {
            return 0.;
        }
        let primary = owners.len() >= 2;
        let radial = if primary { owners } else { content };
        let n = radial.len() as f64;
        let accurate = self.model.accurate_sum;
        let cx = ordered_sum(radial.iter().map(|p| p.x), accurate) / n;
        let cy = ordered_sum(radial.iter().map(|p| p.y), accurate) / n;
        let spread = ordered_sum(radial.iter().map(|p| (p.x - cx).hypot(p.y - cy)), accurate);
        let [bw, bh] = self.model.board;
        let area = (bounds.right - bounds.left) * (bounds.bottom - bounds.top);
        let center = ((bounds.left + bounds.right) / 2. - bw / 2.).powi(2)
            + ((bounds.top + bounds.bottom) / 2. - bh / 2.).powi(2);
        if primary {
            spread * 2.5 + area * 0.4 + center * 20.
        } else {
            spread * 2.5 + area + center * 0.25
        }
    }

    pub(super) fn total_cost(&self) -> f64 {
        let m = &self.model;
        let [bw, bh] = m.board;
        let mut boundary = 0.;
        let mut edge = 0.;
        let mut supplied = 0.;
        let mut zone = 0.;
        for (p, part) in self.poses.iter().zip(&m.parts) {
            let b = p.body;
            for breach in [
                m.margin - b.left,
                b.right - (bw - m.margin),
                m.margin - b.top,
                b.bottom - (bh - m.margin),
            ] {
                if breach > 0. {
                    boundary += breach.powi(2);
                }
            }
            if part.edge {
                let nearest = b
                    .left
                    .min(bw - b.right)
                    .min(b.top)
                    .min(bh - b.bottom)
                    .max(0.);
                let target = m.margin + if part.rf { 3. } else { 1. };
                edge += (nearest - target).max(0.).powi(2) * if part.rf { 20. } else { 8. };
            }
            zone += (p.x - part.zone[0]).hypot(p.y - part.zone[1]) * 0.1 * part.pull;
            for &[l, t, r, bot] in &m.keepouts {
                if b.right > l && b.left < r && b.bottom > t && b.top < bot {
                    supplied += 100000.;
                }
            }
        }
        for (i, side) in &m.edge_rules {
            let b = self.poses[*i].body;
            let dist = match side.as_str() {
                "left" => b.left - m.margin,
                "right" => bw - m.margin - b.right,
                "top" => b.top - m.margin,
                _ => bh - m.margin - b.bottom,
            };
            supplied += dist.max(0.).powi(2) * 2000.;
        }
        for &(a, b, max) in &m.affinities {
            supplied += (self.poses[a].distance(self.poses[b]) - max)
                .max(0.)
                .powi(2)
                * 5000.;
        }
        let (family, clearance) = self.family_costs();
        let (groups, peers) = self.grouped();
        let labels = self.missing_slots.iter().sum::<usize>() as f64 * 250.;
        let mut total = ordered_sum(self.overlaps.values().copied(), m.accurate_sum) * 1000.
            + boundary * 500.
            + family
            + clearance
            + edge
            + groups
            + peers
            + labels
            + supplied;
        if matches!(m.strategy.as_str(), "thermal" | "balanced") {
            let mut thermal = 0.;
            for (i, part) in m.parts.iter().enumerate().filter(|(_, p)| p.heat > 0.1) {
                for (j, other) in m
                    .parts
                    .iter()
                    .enumerate()
                    .skip(i + 1)
                    .filter(|(_, p)| p.heat > 0.1)
                {
                    thermal += (part.heat + other.heat)
                        * (15. - self.poses[i].distance(self.poses[j])).max(0.);
                }
            }
            total += thermal * 10.;
        }
        if matches!(m.strategy.as_str(), "si" | "balanced") {
            total += self.connectivity();
            let mut noise = 0.;
            for (i, _) in m.parts.iter().enumerate().filter(|(_, p)| p.noisy) {
                for (j, _) in m.parts.iter().enumerate().filter(|(_, p)| p.sensitive) {
                    noise += (12. - self.poses[i].distance(self.poses[j]))
                        .max(0.)
                        .powi(2)
                        * 8.;
                }
            }
            total += noise;
        }
        if matches!(m.strategy.as_str(), "cost" | "balanced") {
            total += zone;
            total += self.compactness();
        }
        total
    }
}

struct Bounds {
    left: f64,
    right: f64,
    top: f64,
    bottom: f64,
}
impl Default for Bounds {
    fn default() -> Self {
        Self {
            left: f64::INFINITY,
            right: f64::NEG_INFINITY,
            top: f64::INFINITY,
            bottom: f64::NEG_INFINITY,
        }
    }
}
impl Bounds {
    fn add(&mut self, x: f64, y: f64) {
        self.left = self.left.min(x);
        self.right = self.right.max(x);
        self.top = self.top.min(y);
        self.bottom = self.bottom.max(y);
    }
}
