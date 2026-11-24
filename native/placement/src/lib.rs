mod geometry;
mod model;
mod score;

use geometry::{label_slots, Pose};
use model::Model;
use pyo3::{exceptions::PyValueError, prelude::*};
use std::collections::BTreeMap;

type Update = (usize, f64, f64, f64, f64, f64, f64);

#[pyclass]
struct PlacementState {
    model: Model,
    poses: Vec<Pose>,
    initialized: bool,
    world: Vec<(f64, f64)>,
    point_ids: Vec<Vec<usize>>,
    // Canonical pair ordering preserves Python's accumulation order.
    overlaps: BTreeMap<(usize, usize), f64>,
    missing_slots: Vec<usize>,
}

#[pymethods]
impl PlacementState {
    #[new]
    fn new(model_json: &str) -> PyResult<Self> {
        let model: Model =
            serde_json::from_str(model_json).map_err(|e| PyValueError::new_err(e.to_string()))?;
        if !model.valid_indices() {
            return Err(PyValueError::new_err("Invalid placement topology"));
        }
        let n = model.parts.len();
        let mut point_ids = vec![vec![]; n];
        for (id, &(i, _, _)) in model.points.iter().enumerate() {
            point_ids[i].push(id);
        }
        let world = vec![(0., 0.); model.points.len()];
        Ok(Self {
            model,
            poses: vec![Pose::default(); n],
            initialized: false,
            world,
            point_ids,
            overlaps: BTreeMap::new(),
            missing_slots: vec![0; n],
        })
    }

    fn score(&mut self, updates: Vec<Update>) -> PyResult<f64> {
        let n = self.poses.len();
        let mut changed = vec![false; n];
        for &(i, x, y, w, h, c, s) in &updates {
            if i >= n
                || changed[i]
                || ![x, y, w, h, c, s].iter().all(|v| v.is_finite())
                || w < 0.
                || h < 0.
            {
                return Err(PyValueError::new_err("Invalid or duplicate footprint pose"));
            }
            changed[i] = true;
        }
        if !self.initialized && updates.len() != n {
            return Err(PyValueError::new_err(
                "First score requires every footprint pose",
            ));
        }
        let mut moved = Vec::with_capacity(updates.len());
        for (i, x, y, w, h, c, s) in updates {
            let old = self.poses[i].courtyard;
            self.poses[i] = Pose::new(x, y, w, h, self.model.gap, self.model.parts[i].text);
            moved.push((i, old));
            for &id in &self.point_ids[i] {
                let (_, ox, oy) = self.model.points[id];
                self.world[id] = (x + ox * c + oy * s, y - ox * s + oy * c);
            }
        }
        for &(i, _) in &moved {
            let a = self.poses[i].gap_box;
            for (j, pose) in self.poses.iter().enumerate() {
                if i == j || changed[j] && j < i {
                    continue;
                }
                let b = pose.gap_box;
                let key = (i.min(j), i.max(j));
                if a.intersects(b) && !self.opposite_surfaces(i, j) {
                    let area = (a.right.min(b.right) - a.left.max(b.left))
                        * (a.bottom.min(b.bottom) - a.top.max(b.top));
                    self.overlaps.insert(key, area);
                } else {
                    self.overlaps.remove(&key);
                }
            }
        }
        for (i, part) in self.model.parts.iter().enumerate() {
            let Some(text) = part.text else { continue };
            let pose = self.poses[i];
            let reach = pose.label_neighborhood;
            if !changed[i]
                && !moved.iter().any(|&(j, old)| {
                    self.model.parts[j].layer == part.layer
                        && (reach.intersects(old) || reach.intersects(self.poses[j].courtyard))
                })
            {
                continue;
            }
            let obstacles = self
                .poses
                .iter()
                .enumerate()
                .filter(|(j, p)| {
                    i != *j
                        && reach.intersects(p.courtyard)
                        && self.model.parts[*j].layer == part.layer
                })
                .map(|(j, p)| (p.courtyard, self.model.parts[j].prefix == part.prefix))
                .collect::<Vec<_>>();
            self.missing_slots[i] = 2 - label_slots(pose, text, self.model.board, &obstacles);
        }
        self.initialized = true;
        Ok(self.total_cost())
    }
}

impl PlacementState {
    fn opposite_surfaces(&self, i: usize, j: usize) -> bool {
        let a = &self.model.parts[i];
        let b = &self.model.parts[j];
        a.layer != b.layer && matches!((&a.surface,&b.surface),(Some(x),Some(y)) if x!=y)
    }
}

#[pymodule]
fn circuit_weaver_placement(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("MODEL_VERSION", 1)?;
    m.add_class::<PlacementState>()
}
