const LitElement = Object.getPrototypeOf(
  customElements.get("ha-panel-lovelace")
);
const html = LitElement.prototype.html;
const css = LitElement.prototype.css;

class AdaptiveThermostatCard extends LitElement {
  static get properties() {
    return {
      hass: { type: Object },
      config: { type: Object }
    };
  }

  static getConfigElement() {
    return document.createElement("adaptive-thermostat-card-editor");
  }

  static getStubConfig(hass) {
    // Find the first climate entity
    const climateEntities = Object.keys(hass.states)
      .filter(entityId => entityId.startsWith('climate.'));
    
    return { 
      entity: climateEntities.length > 0 ? climateEntities[0] : '',
      name: "Adaptive Thermostat" 
    };
  }

  setConfig(config) {
    if (!config.entity || !config.entity.startsWith('climate.')) {
      throw new Error('Please specify a climate entity');
    }
    this.config = config;
  }

  // Improve temperature changes to be less laggy
  _increaseTemperature() {
    const entityId = this.config.entity;
    const climate = this.hass.states[entityId];
    
    // If climate has no temperature attribute, set a default
    let currentTemp = climate.attributes.temperature;
    if (currentTemp === undefined) {
      currentTemp = climate.attributes.current_temperature || 20;
    }
    
    const increment = 0.1;
    const newTemp = currentTemp + increment;
    
    // If climate is on, use standard service call
    if (climate.state !== 'off') {
      this.hass.callService('climate', 'set_temperature', {
        entity_id: entityId,
        temperature: newTemp
      });
    } else {
      // If climate is off, update temperature attribute directly
      this.hass.callService('climate', 'set_temperature', {
        entity_id: entityId,
        temperature: newTemp
      });
      // The above still works for many climate integrations even when off
      // The key is NOT calling set_hvac_mode which would turn it on
    }
  }

  _decreaseTemperature() {
    const entityId = this.config.entity;
    const climate = this.hass.states[entityId];
    
    // If climate has no temperature attribute, set a default
    let currentTemp = climate.attributes.temperature;
    if (currentTemp === undefined) {
      currentTemp = climate.attributes.current_temperature || 20;
    }
    
    const decrement = 0.1;
    const newTemp = currentTemp - decrement;
    
    // If climate is on, use standard service call
    if (climate.state !== 'off') {
      this.hass.callService('climate', 'set_temperature', {
        entity_id: entityId,
        temperature: newTemp
      });
    } else {
      // If climate is off, update temperature attribute directly
      this.hass.callService('climate', 'set_temperature', {
        entity_id: entityId,
        temperature: newTemp
      });
      // The above still works for many climate integrations even when off
      // The key is NOT calling set_hvac_mode which would turn it on
    }
  }

  // Rename _togglePower to _turnOff and modify to only turn off
  _turnOff(e) {
    if (e) {
      e.stopPropagation();
      e.preventDefault();
    }
    
    const entityId = this.config.entity;
    const climate = this.hass.states[entityId];
    
    if (!climate) {
      console.log("Climate entity not found:", entityId);
      return;
    }
    
    // No longer check current state - always turn off
    console.log("Turning off thermostat");
    this.hass.callService('climate', 'set_hvac_mode', {
      entity_id: entityId,
      hvac_mode: 'off'
    });
  }

  // Handle preset mode changes
  _setPreset(preset) {
    const entityId = this.config.entity;
    
    // Don't turn off for any preset, just set the preset mode
    this.hass.callService('climate', 'set_preset_mode', {
      entity_id: entityId,
      preset_mode: preset
    });
  }

  _getPresetIcon(preset) {
    switch (preset.toLowerCase()) {
      case 'home':
        return 'mdi:home';
      case 'away':
        return 'mdi:account-arrow-right';
      case 'sleep':
        return 'mdi:sleep';
      default:
        return 'mdi:thermostat';
    }
  }

  _formatPresetName(preset) {
    return preset.charAt(0).toUpperCase() + preset.slice(1);
  }

  _handleCardClick(e) {
    // Prevent clicks on buttons and interactive elements from triggering card click
    if (e.target.tagName === 'BUTTON' || 
        e.target.tagName === 'HA-ICON' ||
        e.target.closest('button') ||
        e.target.closest('.temp-button') ||
        e.target.closest('.power-button') ||
        e.target.closest('.preset')) {
      return;
    }
    
    // Fire the more-info event to open the entity popup
    const entityId = this.config.entity;
    const event = new CustomEvent('hass-more-info', {
      detail: { entityId },
      bubbles: true,
      composed: true
    });
    this.dispatchEvent(event);
  }

  render() {
    if (!this.config || !this.hass) {
      return html`<ha-card><div class="loading">Loading...</div></ha-card>`;
    }

    const entityId = this.config.entity;
    const climate = this.hass.states[entityId];
    
    if (!climate) {
      return html`
        <ha-card>
          <div class="warning">Entity ${entityId} not found.</div>
        </ha-card>
      `;
    }

    const name = this.config.name || climate.attributes.friendly_name || '';
    const isOn = climate.state !== 'off';
    const isHeating = isOn && climate.attributes.hvac_action === 'heating';
    const currentTemp = climate.attributes.current_temperature;
    const targetTemp = climate.attributes.temperature;
    const currentPreset = climate.attributes.preset_mode;
    const presets = climate.attributes.preset_modes || [];
    
    // Get related sensor entity IDs from climate attributes
    const humiditySensorId = climate.attributes.humidity_sensor;
    const outdoorSensorId = climate.attributes.outdoor_sensor;
    const weatherSensorId = climate.attributes.weather_sensor;
    const motionSensorId = climate.attributes.motion_sensor;
    const doorWindowSensorId = climate.attributes.door_window_sensor;
    
    // Only get sensor states if the sensors are configured and exist
    const humiditySensor = humiditySensorId && this.hass.states[humiditySensorId] 
                           ? this.hass.states[humiditySensorId] : null;
    const outdoorSensor = outdoorSensorId && this.hass.states[outdoorSensorId]
                          ? this.hass.states[outdoorSensorId] : null;
    const weatherSensor = weatherSensorId && this.hass.states[weatherSensorId]
                          ? this.hass.states[weatherSensorId] : null;
    const motionSensor = motionSensorId && this.hass.states[motionSensorId]
                         ? this.hass.states[motionSensorId] : null;
    const doorWindowSensor = doorWindowSensorId && this.hass.states[doorWindowSensorId]
                             ? this.hass.states[doorWindowSensorId] : null;

    return html`
      <ha-card @click="${this._handleCardClick}">
        <div class="card-content">
          <div class="header">
            <div class="name">${name}</div>
            <div class="status-container">
              <div class="power-status">
                ${isOn ? 
                  html`On ${isHeating ? html`• Heating` : html`• Idle`}` : 
                  'Off'}
              </div>
            </div>
          </div>
          
          <!-- Main control panel -->
          <div class="control-panel">
            <!-- Current temperature and humidity side by side -->
            <div class="current-readings">
              <div class="current-temperature">
                <div class="label">Current</div>
                <div class="value">
                  ${currentTemp !== undefined 
                    ? html`${currentTemp}<span class="unit">°</span>` 
                    : html`--<span class="unit">°</span>`}
                </div>
              </div>
              
              ${humiditySensor && humiditySensor.state ? html`
                <div class="humidity">
                  <div class="label">Humidity</div>
                  <div class="value">
                    ${humiditySensor.state}<span class="unit">%</span>
                  </div>
                </div>
              ` : ''}
            </div>
          </div>
          
          <!-- Temperature control row -->
          <div class="temp-control-row">
            <!-- Temperature down button -->
            <button class="temp-button" @click="${this._decreaseTemperature}">
              <ha-icon icon="mdi:minus"></ha-icon>
            </button>
            
            <!-- Target temperature - show even when off -->
            <div class="target-temperature">
              <div class="label">Target</div>
              <div class="value">
                ${targetTemp !== undefined
                  ? html`${targetTemp}<span class="unit">°</span>` 
                  : html`--<span class="unit">°</span>`}
              </div>
            </div>
            
            <!-- Temperature up button -->
            <button class="temp-button" @click="${this._increaseTemperature}">
              <ha-icon icon="mdi:plus"></ha-icon>
            </button>
          </div>
          
          <!-- New control buttons layout -->
          <div class="control-buttons">
            <!-- Heat flame button (on left) -->
            <button 
              class="control-button flame-button ${isOn ? 'active' : 'inactive'}" 
              @click="${this._toggleHeat}"
            >
              <ha-icon icon="mdi:fire"></ha-icon>
              <span>Heat</span>
            </button>
            
            <!-- Power button (on right) - now calls _turnOff instead of _togglePower -->
            <button 
              class="control-button power-button ${isOn ? 'inactive' : 'active'}" 
              @click="${this._turnOff}"
            >
              <ha-icon icon="mdi:power"></ha-icon>
              <span>Power</span>
            </button>
          </div>
          
          <!-- Sensors -->
          ${this._renderSensors(null, outdoorSensor, weatherSensor, motionSensor, doorWindowSensor)}
          
          <!-- Presets in specified order: away, home, sleep -->
          ${presets && presets.length > 0 ? html`
            <div class="presets">
              ${this._renderPresets(presets, currentPreset)}
            </div>
          ` : ''}
        </div>
      </ha-card>
    `;
  }

  // New method to render presets in the desired order
  _renderPresets(presets, currentPreset) {
    // Define the desired preset order
    const presetOrder = ['away', 'home', 'sleep'];
    
    // Filter and sort presets according to the desired order
    const orderedPresets = presetOrder
      .filter(preset => presets.includes(preset))
      .concat(presets.filter(preset => !presetOrder.includes(preset)));
    
    return orderedPresets.map(preset => html`
      <button class="preset ${currentPreset === preset ? 'active' : ''}" 
              @click="${() => this._setPreset(preset)}">
        <ha-icon icon="${this._getPresetIcon(preset)}"></ha-icon>
        <span>${this._formatPresetName(preset)}</span>
      </button>
    `);
  }

  _renderSensors(humiditySensor, outdoorSensor, weatherSensor, motionSensor, doorWindowSensor) {
    const sensors = [];
    
    if (outdoorSensor) {
      sensors.push({
        icon: 'mdi:thermometer-auto',
        label: 'Outdoor',
        value: `${outdoorSensor.state}°`
      });
    }
    
    if (weatherSensor) {
      sensors.push({
        icon: 'mdi:weather-partly-cloudy',
        label: 'Weather',
        value: weatherSensor.state
      });
    }
    
    if (motionSensor) {
      const isActive = motionSensor.state === 'on';
      sensors.push({
        icon: isActive ? 'mdi:motion-sensor' : 'mdi:motion-sensor-off',
        label: 'Motion',
        value: isActive ? 'Active' : 'Clear'
      });
    }
    
    if (doorWindowSensor) {
      const isOpen = doorWindowSensor.state === 'on';
      sensors.push({
        icon: isOpen ? 'mdi:window-open' : 'mdi:window-closed',
        label: 'Window',
        value: isOpen ? 'Open' : 'Closed'
      });
    }
    
    return sensors.length > 0 ? html`
      <div class="sensors">
        ${sensors.map(sensor => html`
          <div class="sensor">
            <ha-icon icon="${sensor.icon}"></ha-icon>
            <div class="value">${sensor.value}</div>
            <div class="label">${sensor.label}</div>
          </div>
        `)}
      </div>
    ` : '';
  }

  // Add new toggle heat function
  _toggleHeat(e) {
    if (e) {
      e.stopPropagation();
      e.preventDefault();
    }
    
    const entityId = this.config.entity;
    const climate = this.hass.states[entityId];
    
    if (!climate) {
      console.log("Climate entity not found:", entityId);
      return;
    }
    
    const currentState = climate.state;
    
    if (currentState === 'off') {
      // Turn on to heat mode
      const availableModes = climate.attributes.hvac_modes || [];
      if (availableModes.includes('heat')) {
        this.hass.callService('climate', 'set_hvac_mode', {
          entity_id: entityId,
          hvac_mode: 'heat'
        });
      } else if (availableModes.length > 0 && availableModes[0] !== 'off') {
        // Fall back to first available non-off mode
        this.hass.callService('climate', 'set_hvac_mode', {
          entity_id: entityId,
          hvac_mode: availableModes.filter(mode => mode !== 'off')[0]
        });
      }
    }
  }

  static get styles() {
    return css`
      ha-card {
        --primary-color: var(--primary, var(--paper-item-icon-color));
        --text-primary-color: var(--primary-text-color);
        --secondary-text-color: var(--secondary-text-color);
        --spacing: 16px;
        --card-border-radius: 12px;
        
        border-radius: var(--card-border-radius);
        padding: 0;
        overflow: hidden;
        cursor: pointer;
      }
      
      .card-content {
        padding: var(--spacing);
      }
      
      .header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: var(--spacing);
        position: relative;
      }
      
      .name {
        font-size: 1.5rem;
        font-weight: 500;
        color: var(--text-primary-color);
      }
      
      .status-container {
        display: flex;
        align-items: center;
      }
      
      .power-status {
        font-size: 0.9rem;
        font-weight: 500;
        color: var(--text-primary-color);
      }
      
      /* Main control panel layout */
      .control-panel {
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: var(--spacing);
      }
      
      .current-readings {
        display: flex;
        align-items: flex-end;
        gap: 24px;
        width: 100%;
        justify-content: center;
      }
      
      .current-temperature, .humidity {
        text-align: center;
      }
      
      .current-temperature .value {
        font-size: 3.5rem;
        font-weight: 300;
        line-height: 1;
      }
      
      .current-temperature .unit {
        font-size: 2.5rem;
        font-weight: 300;
        opacity: 0.8;
      }
      
      /* Make humidity match temperature styling */
      .humidity .value {
        font-size: 3.5rem;
        font-weight: 300;
        line-height: 1;
      }
      
      .humidity .unit {
        font-size: 2.5rem;
        font-weight: 300;
        opacity: 0.8;
      }
      
      /* New control buttons styles */
      .control-buttons {
        display: flex;
        justify-content: space-between;
        gap: var(--spacing);
        margin-top: var(--spacing);
        margin-bottom: var(--spacing);
      }
      
      .control-button {
        flex: 1;
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        padding: 12px;
        border: none;
        border-radius: 12px;
        background: var(--ha-card-background, var(--card-background-color));
        box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        transition: all 0.3s ease;
        cursor: pointer;
      }
      
      .control-button span {
        margin-top: 4px;
        font-size: 0.9rem;
      }
      
      .flame-button.active {
        background-color: #ffab40;
        color: white;
      }
      
      .flame-button.inactive {
        background-color: #f5f5f5;
        color: #9e9e9e;
      }
      
      .power-button.active {
        background-color: #2196f3;
        color: white;
      }
      
      .power-button.inactive {
        background-color: #f5f5f5;
        color: #9e9e9e;
      }
      
      /* Temperature control row */
      .temp-control-row {
        display: flex;
        justify-content: space-around;
        align-items: center;
        margin-bottom: var(--spacing);
        padding: var(--spacing) 0;
        background-color: rgba(var(--rgb-primary-color, 0, 134, 196), 0.05);
        border-radius: 12px;
      }
      
      .target-temperature {
        text-align: center;
      }
      
      .target-temperature .value {
        font-size: 2.2rem;
        font-weight: 400;
        color: var(--primary-color);
      }
      
      .target-temperature .unit {
        font-size: 1.6rem;
        opacity: 0.8;
      }
      
      .temp-button {
        width: 40px;
        height: 40px;
        border-radius: 50%;
        display: flex;
        align-items: center;
        justify-content: center;
        border: none;
        background-color: rgba(var(--rgb-primary-color, 0, 134, 196), 0.1);
        color: var(--primary-color);
        cursor: pointer;
        transition: background-color 0.3s;
      }
      
      .temp-button:hover {
        background-color: rgba(var(--rgb-primary-color, 0, 134, 196), 0.2);
      }
      
      .label {
        font-size: 0.9rem;
        color: var(--secondary-text-color);
        margin-top: 4px;
      }
      
      .sensors {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(100px, 1fr));
        gap: var(--spacing);
        margin-bottom: var(--spacing);
      }
      
      .sensor {
        display: flex;
        flex-direction: column;
        align-items: center;
        text-align: center;
        background-color: rgba(var(--rgb-primary-color, 0, 134, 196), 0.05);
        padding: 12px;
        border-radius: 12px;
      }
      
      .sensor ha-icon {
        color: var(--primary-color);
        margin-bottom: 8px;
      }
      
      .sensor .value {
        font-weight: 500;
        margin-bottom: 4px;
        color: var(--text-primary-color);
      }
      
      .sensor .label {
        font-size: 0.85rem;
        color: var(--secondary-text-color);
      }
      
      .presets {
        display: flex;
        justify-content: space-around;
        flex-wrap: wrap;
        gap: 8px;
      }
      
      .preset {
        flex: 1;
        min-width: 80px;
        background-color: var(--card-background-color);
        border: 1px solid rgba(var(--rgb-primary-color, 0, 134, 196), 0.2);
        border-radius: 8px;
        padding: 8px;
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        cursor: pointer;
        transition: all 0.3s;
      }
      
      .preset.active {
        background-color: var(--primary-color);
        color: white;
        border-color: var(--primary-color);
      }
      
      .preset:hover:not(.active) {
        background-color: rgba(var(--rgb-primary-color, 0, 134, 196), 0.1);
      }
      
      .preset ha-icon {
        margin-bottom: 4px;
      }
      
      .warning {
        padding: 20px;
        text-align: center;
        color: var(--error-color);
      }
      
      .loading {
        padding: 20px;
        text-align: center;
        color: var(--secondary-text-color);
      }
      
      /* Make buttons and interactive elements retain their specific cursors */
      button {
        cursor: pointer;
      }
    `;
  }
}

// Card editor
class AdaptiveThermostatCardEditor extends LitElement {
  static get properties() {
    return {
      hass: { type: Object },
      config: { type: Object }
    };
  }

  setConfig(config) {
    this.config = config;
  }

  render() {
    if (!this.hass || !this.config) {
      return html``;
    }

    return html`
      <ha-form
        .schema=${[
          { name: 'entity', selector: { entity: { domain: 'climate' } } },
          { name: 'name', selector: { text: {} } }
        ]}
        .data=${this.config}
        .hass=${this.hass}
        @value-changed=${this._valueChanged}
      ></ha-form>
    `;
  }

  _valueChanged(ev) {
    const config = {
      ...this.config,
      ...ev.detail.value
    };
    
    const event = new CustomEvent('config-changed', {
      detail: { config },
      bubbles: true,
      composed: true
    });
    this.dispatchEvent(event);
  }
}

customElements.define('adaptive-thermostat-card-editor', AdaptiveThermostatCardEditor);
customElements.define('adaptive-thermostat-card', AdaptiveThermostatCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: 'adaptive-thermostat-card',
  name: 'Adaptive Thermostat Card',
  description: 'A beautiful card for controlling your Adaptive Thermostat',
  preview: true,
}); 