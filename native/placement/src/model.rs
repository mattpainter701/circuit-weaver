use serde::Deserialize;

pub type Leg = Vec<(usize, usize)>;

#[derive(Deserialize)]
pub struct Part {
    pub layer: String,
    pub surface: Option<String>,
    pub prefix: String,
    pub text: Option<[f64; 2]>,
    pub heat: f64,
    pub zone: [f64; 2],
    pub pull: f64,
    pub edge: bool,
    pub rf: bool,
    pub locked: bool,
    pub support: bool,
    pub noisy: bool,
    pub sensitive: bool,
}

#[derive(Deserialize)]
pub struct Family {
    pub child: usize,
    pub parent: Option<usize>,
    pub critical: bool,
    pub shared: Vec<Leg>,
    pub owner_pin: Leg,
    pub supplies: Leg,
    pub grounds: Leg,
}

#[derive(Deserialize)]
pub struct Model {
    pub board: [f64; 2],
    pub margin: f64,
    pub gap: f64,
    pub support_gap: f64,
    pub strategy: String,
    pub accurate_sum: bool,
    pub parts: Vec<Part>,
    pub points: Vec<(usize, f64, f64)>,
    pub families: Vec<Family>,
    pub output_links: Vec<Leg>,
    pub groups: Vec<Vec<usize>>,
    pub peers: Vec<(Vec<usize>, bool)>,
    pub pad_mode: bool,
    pub nets: Vec<(f64, Vec<usize>)>,
    pub edges: Vec<(usize, usize, f64)>,
    pub keepouts: Vec<[f64; 4]>,
    pub edge_rules: Vec<(usize, String)>,
    pub affinities: Vec<(usize, usize, f64)>,
}

impl Model {
    pub fn valid_indices(&self) -> bool {
        let n = self.parts.len();
        let p = self.points.len();
        let valid_leg = |leg: &Leg| leg.iter().all(|&(a, b)| a < p && b < p);
        let valid_group =
            |members: &Vec<usize>| members.len() >= 2 && members.iter().all(|&i| i < n);
        self.points.iter().all(|&(i, _, _)| i < n)
            && self.groups.iter().all(valid_group)
            && self.peers.iter().all(|(members, _)| valid_group(members))
            && self
                .nets
                .iter()
                .all(|(_, ids)| ids.len() >= 2 && ids.iter().all(|&i| i < p))
            && self
                .edges
                .iter()
                .chain(self.affinities.iter())
                .all(|&(a, b, _)| a < n && b < n)
            && self.edge_rules.iter().all(|(i, edge)| {
                *i < n && matches!(edge.as_str(), "top" | "bottom" | "left" | "right")
            })
            && self.output_links.iter().all(valid_leg)
            && self.families.iter().all(|f| {
                f.child < n
                    && f.parent.is_none_or(|i| i < n)
                    && f.shared
                        .iter()
                        .chain([&f.owner_pin, &f.supplies, &f.grounds])
                        .all(valid_leg)
            })
    }
}
