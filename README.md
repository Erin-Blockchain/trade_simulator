TRADING MODEL ANALYSIS DASHBOARD
================================

A lightweight Streamlit web application that visualizes expected value (EV), discrete probability distributions, and profit/loss curves for quantitative trading models.


FEATURES
--------
* Interactive Parameters: Adjust capital, stop-loss, take-profit, and win rates on the fly.
* Statistical Modeling: Calculates discrete outcome probabilities using binomial distribution.
* Data Visualization: Generates combined probability and PnL charts using matplotlib.
* Custom Styling: Built-in support for raw HTML/CSS injection to override default UI elements.
* Export: One-click download of the analysis report as a high-resolution .png file.


QUICK START
-----------
1. Clone the repository and navigate to the project directory:
   git clone <your-repo-url>
   cd <your-repo-directory>

2. Install the required dependencies:
   pip install -r requirements.txt

3. Launch the application:
   streamlit run app.py

The dashboard will automatically open in your default web browser at http://localhost:8501.
