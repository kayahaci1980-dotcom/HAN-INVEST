plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.haninvest.app"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.haninvest.app"
        minSdk = 24
        targetSdk = 35
        versionCode = 10
        versionName = "2.9"
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }
}

dependencies {
    implementation("androidx.work:work-runtime-ktx:2.10.1")
}
