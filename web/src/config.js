// Visual constants shared by the scene and the UI.
export const BG = [10, 13, 18];
export const GROUND = [22, 27, 35];
export const FADE = 0.22; // share of the scene radius used for the fade-out at the rim
export const MAX_PIXEL_RATIO = 1.5; // cap HiDPI rendering: 2x+ quadruples fill cost for little gain

export const ROAD = {
  trunk: [150, 164, 186], primary: [150, 164, 186], secondary: [124, 138, 160], tertiary: [102, 115, 136],
};
export const ROAD_DEFAULT = [78, 89, 107];
export const MAJOR = new Set(['trunk', 'primary', 'secondary', 'tertiary']);
export const ROAD_RU = {
  trunk: 'магистраль', primary: 'главная улица', secondary: 'районная улица', tertiary: 'местная улица',
  residential: 'жилая улица', living_street: 'жилая зона', unclassified: 'проезд',
};

export const BUILDING = {
  apartments: [96, 108, 128], residential: [92, 102, 120], house: [104, 98, 96], detached: [104, 98, 96],
  school: [118, 110, 96], university: [118, 110, 96], kindergarten: [118, 110, 96], hospital: [112, 118, 124],
  retail: [110, 104, 122], commercial: [100, 108, 130], industrial: [86, 90, 98], mosque: [104, 122, 118],
};
export const BUILDING_DEFAULT = [90, 100, 116];
export const BUILDING_RU = {
  apartments: 'Жилой дом', residential: 'Жилой дом', house: 'Частный дом', detached: 'Частный дом',
  school: 'Школа', university: 'Университет', kindergarten: 'Детский сад', hospital: 'Больница',
  retail: 'Магазин', commercial: 'Коммерческое здание', industrial: 'Промышленное здание', mosque: 'Мечеть',
  garage: 'Гараж', garages: 'Гаражи', office: 'Офисное здание', hotel: 'Гостиница', public: 'Общественное здание',
};

export const AREA = { green: [34, 62, 48], water: [30, 58, 88], pitch: [44, 82, 58], parking: [34, 40, 50] };
export const MINOR = {
  service: { width: 3.4, color: [56, 65, 79] }, path: { width: 1.6, color: [86, 82, 70] },
  water: { width: 4, color: [38, 74, 110] }, rail: { width: 2, color: [120, 110, 110] },
};

export const POI = {
  hospital: { label: 'Больница', color: [255, 110, 130] },
  clinic: { label: 'Клиника', color: [255, 110, 130] },
  police: { label: 'Полиция', color: [110, 150, 255] },
  fire_station: { label: 'Пожарная часть', color: [255, 150, 70] },
};

export const STATUS = { ok: [70, 190, 150], warn: [255, 190, 70], alert: [255, 80, 80] };

// vehicle kinds by index: [length, width, height] in metres and colour
export const KINDS = ['car', 'bus', 'emg'];
export const VEHICLE_SIZE = [[4.4, 1.9, 1.5], [11.5, 2.6, 3.2], [6.2, 2.2, 2.7]];
export const VEHICLE_COLOR = [[92, 200, 255], [255, 168, 38], [255, 60, 60]];
export const EMG_BLINK = [[255, 60, 60], [80, 130, 255]];
